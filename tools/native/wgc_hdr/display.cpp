#include <windows.h>
#include <dispatcherqueue.h>
#include <dxgi1_6.h>
#include <windows.graphics.display.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Display.h>
#include <winrt/Windows.System.h>
#include <atomic>
#include <thread>
#include <vector>

using namespace winrt;
using namespace winrt::Windows::Graphics::Display;

namespace {
// DXGI distinguishes HDR from SDR with wide-gamut color management on older Windows.
int legacy_mode(HMONITOR monitor) {
    com_ptr<IDXGIFactory1> factory;
    check_hresult(CreateDXGIFactory1(guid_of<IDXGIFactory1>(), factory.put_void()));
    for (UINT a = 0;; ++a) {
        com_ptr<IDXGIAdapter1> adapter;
        auto hr = factory->EnumAdapters1(a, adapter.put());
        if (hr == DXGI_ERROR_NOT_FOUND) break;
        check_hresult(hr);
        for (UINT o = 0;; ++o) {
            com_ptr<IDXGIOutput> output;
            hr = adapter->EnumOutputs(o, output.put());
            if (hr == DXGI_ERROR_NOT_FOUND) break;
            check_hresult(hr);
            DXGI_OUTPUT_DESC desc{};
            if (FAILED(output->GetDesc(&desc)) || desc.Monitor != monitor) continue;
            auto out6 = output.try_as<IDXGIOutput6>();
            if (!out6) throw_hresult(E_NOINTERFACE);
            DXGI_OUTPUT_DESC1 color{};
            check_hresult(out6->GetDesc1(&color));
            return color.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 ? 1 : 0;
        }
    }
    throw_hresult(DXGI_ERROR_NOT_FOUND);
}

DISPLAYCONFIG_PATH_TARGET_INFO display_target(const wchar_t* name) {
    for (int attempt = 0; attempt != 3; ++attempt) {
        UINT32 np{}, nm{};
        auto error = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm);
        if (error != ERROR_SUCCESS) throw_hresult(HRESULT_FROM_WIN32(error));
        std::vector<DISPLAYCONFIG_PATH_INFO> paths(np);
        std::vector<DISPLAYCONFIG_MODE_INFO> modes(nm);
        error = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths.data(), &nm, modes.data(), nullptr);
        if (error == ERROR_INSUFFICIENT_BUFFER) continue;
        if (error != ERROR_SUCCESS) throw_hresult(HRESULT_FROM_WIN32(error));
        for (UINT32 i = 0; i < np; ++i) {
            const auto& p = paths[i];
            DISPLAYCONFIG_SOURCE_DEVICE_NAME src{};
            src.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME, sizeof(src), p.sourceInfo.adapterId, p.sourceInfo.id};
            if (DisplayConfigGetDeviceInfo(&src.header) == ERROR_SUCCESS && !wcscmp(src.viewGdiDeviceName, name))
                return p.targetInfo;
        }
        break;
    }
    throw_hresult(HRESULT_FROM_WIN32(ERROR_NOT_FOUND));
}

struct ObserverState {
    std::atomic<uint64_t> epoch{1};
    std::atomic<bool> reliable{false};
};
struct Watch;
thread_local Watch* current_watch{};

struct Watch {
    HWND target{};
    HMONITOR monitor{};
    HWND message_window{};
    HWINEVENTHOOK hook{};
    handle stop{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    handle initialized{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    std::shared_ptr<ObserverState> state = std::make_shared<ObserverState>();
    winrt::Windows::System::DispatcherQueueController queue{nullptr};
    DisplayInformation info{nullptr};
    DisplayInformation::AdvancedColorInfoChanged_revoker changed;
    std::thread worker;

    explicit Watch(HWND hwnd) : target(hwnd) {
        check_bool(bool(stop) && bool(initialized));
        worker = std::thread([this] { run(); });
        // The first consumer normally sees the settled observer epoch, not a startup invalidation.
        WaitForSingleObject(initialized.get(), 300);
    }
    ~Watch() { SetEvent(stop.get()); worker.join(); }

    void refresh_monitor(bool force) noexcept {
        const auto now = MonitorFromWindow(target, MONITOR_DEFAULTTONEAREST);
        if (!force && monitor == now) return;
        monitor = now;
        changed.revoke();
        info = nullptr;
        state->reliable.store(false);
        if (queue && monitor) {
            try {
                const auto factory = get_activation_factory<DisplayInformation, IDisplayInformationStaticsInterop>();
                check_hresult(factory->GetForMonitor(monitor, guid_of<DisplayInformation>(), put_abi(info)));
                changed = info.AdvancedColorInfoChanged(auto_revoke, [s = state](auto&&, auto&&) { ++s->epoch; });
                state->reliable.store(hook != nullptr && message_window != nullptr);
            } catch (...) {}  // Old Windows: caller uses a bounded, demand-only refresh.
        }
        ++state->epoch;
    }

    static void CALLBACK on_move(HWINEVENTHOOK, DWORD, HWND hwnd, LONG object, LONG child, DWORD, DWORD) {
        if (current_watch && hwnd == current_watch->target && object == OBJID_WINDOW && child == CHILDID_SELF)
            current_watch->refresh_monitor(false);
    }
    static LRESULT CALLBACK on_message(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
        if (current_watch && (msg == WM_DISPLAYCHANGE || (msg == WM_POWERBROADCAST &&
                (wp == PBT_APMRESUMEAUTOMATIC || wp == PBT_APMRESUMESUSPEND))))
            current_watch->refresh_monitor(true);
        return DefWindowProcW(hwnd, msg, wp, lp);
    }

    void run() noexcept {
        try { init_apartment(apartment_type::single_threaded); }
        catch (...) { SetEvent(initialized.get()); return; }
        current_watch = this;
        try {
            DispatcherQueueOptions options{sizeof(options), DQTYPE_THREAD_CURRENT, DQTAT_COM_NONE};
            if (FAILED(CreateDispatcherQueueController(options,
                    reinterpret_cast<ABI::Windows::System::IDispatcherQueueController**>(put_abi(queue))))) queue = nullptr;
            WNDCLASSW wc{};
            wc.lpfnWndProc = on_message;
            wc.lpszClassName = L"AuraWgcHdrDisplayObserver";
            wc.hInstance = GetModuleHandleW(nullptr);
            RegisterClassW(&wc);
            message_window = CreateWindowExW(0, wc.lpszClassName, L"", WS_POPUP, 0, 0, 0, 0,
                                             nullptr, nullptr, wc.hInstance, nullptr);
            DWORD pid{};
            GetWindowThreadProcessId(target, &pid);
            if (pid) hook = SetWinEventHook(EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, nullptr,
                                            on_move, pid, 0, WINEVENT_OUTOFCONTEXT);
            refresh_monitor(true);
            SetEvent(initialized.get());
            const HANDLE stop_event = stop.get();
            while (MsgWaitForMultipleObjects(1, &stop_event, FALSE, INFINITE, QS_ALLINPUT) == WAIT_OBJECT_0 + 1) {
                MSG msg{};
                while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) DispatchMessageW(&msg);
            }
        } catch (...) {}
        SetEvent(initialized.get());
        state->reliable.store(false);
        ++state->epoch;
        if (hook) UnhookWinEvent(hook);
        if (message_window) DestroyWindow(message_window);
        changed.revoke();
        info = nullptr;
        try {
            if (queue) {
                auto shutdown = queue.ShutdownQueueAsync();
                const auto end = GetTickCount64() + 500;
                while (shutdown.Status() == winrt::Windows::Foundation::AsyncStatus::Started && GetTickCount64() < end) {
                    MsgWaitForMultipleObjects(0, nullptr, FALSE, 20, QS_ALLINPUT);
                    MSG msg{};
                    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) DispatchMessageW(&msg);
                }
            }
        } catch (...) {}
        queue = nullptr;
        current_watch = nullptr;
        uninit_apartment();
    }
};
}

extern "C" __declspec(dllexport) int32_t aura_hdr_display_query(uintptr_t hwnd, uint32_t* white) noexcept {
    try {
        const auto monitor = MonitorFromWindow(reinterpret_cast<HWND>(hwnd), MONITOR_DEFAULTTONEAREST);
        MONITORINFOEXW info{};
        info.cbSize = sizeof info;
        check_bool(GetMonitorInfoW(monitor, &info));
        const auto path = display_target(info.szDevice);
        DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO_2 color{};
        color.header = {DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO_2, sizeof color, path.adapterId, path.id};
        const auto error = DisplayConfigGetDeviceInfo(&color.header);
        const bool hdr = error == ERROR_SUCCESS ? color.activeColorMode == DISPLAYCONFIG_ADVANCED_COLOR_MODE_HDR : legacy_mode(monitor) == 1;
        *white = 0;
        if (hdr) {
            DISPLAYCONFIG_SDR_WHITE_LEVEL level{};
            level.header = {DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL, sizeof level, path.adapterId, path.id};
            const auto rc = DisplayConfigGetDeviceInfo(&level.header);
            if (rc != ERROR_SUCCESS) throw_hresult(HRESULT_FROM_WIN32(rc));
            if (!level.SDRWhiteLevel) throw_hresult(E_UNEXPECTED);
            *white = level.SDRWhiteLevel;
        }
        return 0;
    } catch (...) { return to_hresult(); }
}

extern "C" __declspec(dllexport) void* aura_hdr_watch_open(uintptr_t hwnd) noexcept {
    try { return new Watch(reinterpret_cast<HWND>(hwnd)); } catch (...) { return nullptr; }
}
extern "C" __declspec(dllexport) uint64_t aura_hdr_watch_epoch(void* p) noexcept {
    const auto& s = static_cast<Watch*>(p)->state;
    return (s->epoch.load() << 1) | uint64_t(s->reliable.load());
}
extern "C" __declspec(dllexport) void aura_hdr_watch_close(void* p) noexcept { delete static_cast<Watch*>(p); }
