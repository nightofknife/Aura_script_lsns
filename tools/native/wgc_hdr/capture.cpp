#include <windows.h>
#include <roapi.h>
#include <d3d11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Foundation.Metadata.h>
#include <DirectXPackedVector.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <memory>

using namespace winrt;
using namespace winrt::Windows::Graphics::Capture;
using namespace winrt::Windows::Graphics::DirectX;
using namespace winrt::Windows::Graphics::DirectX::Direct3D11;

namespace {
struct Signal {
    handle ready{CreateEventW(nullptr, FALSE, FALSE, nullptr)};
    std::atomic<bool> closed{false};
};

struct Capture {
    std::shared_ptr<Signal> signal = std::make_shared<Signal>();
    com_ptr<ID3D11Device> device;
    com_ptr<ID3D11DeviceContext> context;
    IDirect3DDevice device_rt{nullptr};
    Direct3D11CaptureFramePool pool{nullptr};
    Direct3D11CaptureFrame pending{nullptr};
    GraphicsCaptureSession session{nullptr};
    GraphicsCaptureItem item{nullptr};
    Direct3D11CaptureFramePool::FrameArrived_revoker arrived;
    GraphicsCaptureItem::Closed_revoker closed;
    com_ptr<ID3D11Texture2D> staging;
    UINT texture_width{}, texture_height{};
    uint32_t last_white{};
    std::array<uint8_t, 65536> lut{};

    Capture(HWND hwnd, bool cursor) {
        check_bool(bool(signal->ready));
        check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                      nullptr, 0, D3D11_SDK_VERSION, device.put(), nullptr, context.put()));
        com_ptr<::IInspectable> wrapped;
        check_hresult(CreateDirect3D11DeviceFromDXGIDevice(device.as<IDXGIDevice>().get(), wrapped.put()));
        device_rt = wrapped.as<IDirect3DDevice>();
        const auto factory = get_activation_factory<GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
        check_hresult(factory->CreateForWindow(hwnd, guid_of<GraphicsCaptureItem>(), put_abi(item)));
        pool = Direct3D11CaptureFramePool::CreateFreeThreaded(device_rt, DirectXPixelFormat::R16G16B16A16Float, 2, item.Size());
        arrived = pool.FrameArrived(auto_revoke, [state = signal](auto&&, auto&&) { SetEvent(state->ready.get()); });
        closed = item.Closed(auto_revoke, [state = signal](auto&&, auto&&) {
            state->closed.store(true);
            SetEvent(state->ready.get());
        });
        session = pool.CreateCaptureSession(item);
        if (winrt::Windows::Foundation::Metadata::ApiInformation::IsPropertyPresent(
                L"Windows.Graphics.Capture.GraphicsCaptureSession", L"IsCursorCaptureEnabled"))
            session.IsCursorCaptureEnabled(cursor);
        session.StartCapture();
    }

    ~Capture() {
        arrived.revoke();
        closed.revoke();
        pending = nullptr;
        try { if (session) session.Close(); } catch (...) {}
        try { if (pool) pool.Close(); } catch (...) {}
    }

    void set_white(uint32_t white) {
        if (white == last_white) return;
        if (white == 0) throw_hresult(E_INVALIDARG);
        const float gain = 1000.0f / float(white); // Fixed point SDR white, 1000 = 80 nits.
        for (unsigned i = 0; i < lut.size(); ++i) {
            const float value = ::DirectX::PackedVector::XMConvertHalfToFloat(static_cast<uint16_t>(i));
            const float x = std::clamp(std::isnan(value) ? 0.0f : value * gain, 0.0f, 1.0f);
            const float y = x <= .0031308f ? 12.92f * x : 1.055f * std::pow(x, 1.0f / 2.4f) - .055f;
            lut[i] = static_cast<uint8_t>(std::clamp(std::lround(y * 255), 0L, 255L));
        }
        last_white = white;
    }

    int read(uint32_t white, uint8_t* output, size_t capacity, int* width, int* height) {
        if (signal->closed.load()) throw_hresult(RO_E_CLOSED);
        if (!pending) {
            WaitForSingleObject(signal->ready.get(), 32);
            if (signal->closed.load()) throw_hresult(RO_E_CLOSED);
            // Keep the newest of the queued frames. Bound the loop if the compositor is busy.
            for (int i = 0; i != 2; ++i) {
                auto frame = pool.TryGetNextFrame();
                if (!frame) break;
                pending = std::move(frame);
            }
            if (!pending) return 0;
        }
        auto content = pending.ContentSize();
        if (content.Width <= 0 || content.Height <= 0) { pending = nullptr; return 0; }
        auto access = pending.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
        com_ptr<ID3D11Texture2D> src;
        check_hresult(access->GetInterface(guid_of<ID3D11Texture2D>(), src.put_void()));
        D3D11_TEXTURE2D_DESC d{};
        src->GetDesc(&d);
        if (d.Format != DXGI_FORMAT_R16G16B16A16_FLOAT) throw_hresult(E_UNEXPECTED);
        if (UINT(content.Width) != d.Width || UINT(content.Height) != d.Height) {
            // Release the native frame and views before resizing the pool.
            src = nullptr;
            access = nullptr;
            pending = nullptr;
            pool.Recreate(device_rt, DirectXPixelFormat::R16G16B16A16Float, 2, content);
            return 0;
        }
        *width = content.Width;
        *height = content.Height;
        if (size_t(*width) * *height * 4 > capacity) return 2;
        if (!staging || texture_width != d.Width || texture_height != d.Height) {
            staging = nullptr;
            d.Usage = D3D11_USAGE_STAGING;
            d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            d.BindFlags = d.MiscFlags = 0;
            check_hresult(device->CreateTexture2D(&d, nullptr, staging.put()));
            texture_width = d.Width;
            texture_height = d.Height;
        }
        set_white(white);
        context->CopyResource(staging.get(), src.get());
        D3D11_MAPPED_SUBRESOURCE mapped{};
        check_hresult(context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
        for (int y = 0; y < *height; ++y) {
            const auto* row = reinterpret_cast<const uint16_t*>(static_cast<const char*>(mapped.pData) + size_t(y) * mapped.RowPitch);
            for (int x = 0; x < *width; ++x, row += 4, output += 4) {
                output[0] = lut[row[2]];
                output[1] = lut[row[1]];
                output[2] = lut[row[0]];
                output[3] = 255;
            }
        }
        context->Unmap(staging.get(), 0);
        pending = nullptr;
        return 1;
    }
};
}

extern "C" __declspec(dllexport) int32_t aura_hdr_capture_open(uintptr_t hwnd, int cursor, void** result) noexcept {
    const auto hr = RoInitialize(RO_INIT_MULTITHREADED);
    if (FAILED(hr)) return hr;
    try {
        *result = new Capture(reinterpret_cast<HWND>(hwnd), cursor != 0);
        return 0;
    } catch (...) {
        const auto error = to_hresult();
        RoUninitialize();
        return error;
    }
}

extern "C" __declspec(dllexport) int32_t aura_hdr_capture_read(void* p, uint32_t white, uint8_t* data, size_t size,
                                                             int* width, int* height) noexcept {
    try { return static_cast<Capture*>(p)->read(white, data, size, width, height); }
    catch (...) { return to_hresult(); }
}

extern "C" __declspec(dllexport) void aura_hdr_capture_close(void* p) noexcept {
    delete static_cast<Capture*>(p);
    RoUninitialize();
}
