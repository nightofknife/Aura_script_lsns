#include "frame_stream.hpp"
#include <algorithm>
#include <cstring>

namespace bridge {
namespace {
constexpr int kWidth = 1280, kHeight = 720;
constexpr size_t kBytes = size_t(kWidth) * kHeight * 4;
constexpr uint32_t kReady = 1, kViewportChanged = 2, kCaptureFailed = 3;

// Mirrored by bridge/frame_stream.py. A named mutex protects the header and pixels.
struct Header {
  uint32_t state, width, height, bytes;
  uint64_t generation, captured_qpc, unity_frame;
};
static_assert(sizeof(Header) == 40);

struct TryLock {
  HANDLE mutex;
  DWORD result = WaitForSingleObject(mutex, 0);
  ~TryLock() {
    if (result == WAIT_OBJECT_0 || result == WAIT_ABANDONED)
      ReleaseMutex(mutex);
  }
  bool acquired() const { return result == WAIT_OBJECT_0; }
};

struct Texture {
  Root root;
  Class unity_object;
  ~Texture() {
    try {
      if (root.get())
        il.stat(unity_object, "Destroy", {root.get()});
    } catch (...) {
    }
  }
};
} // namespace

void FrameStream::open(const std::string &session, DWORD client_pid, int width, int height) {
  if (enabled())
    return;
  if (width != kWidth || height != kHeight)
    throw Error("bridge_capture_resolution", "Bridge capture requires Unity resolution 1280x720");
  if (session.size() != 32 || session.find_first_not_of("0123456789abcdef") != std::string::npos)
    throw Error("protocol_invalid", "Frame stream requires a bridge session UUID");

  unity_object_ = il.klass("UnityEngine", "Object");
  texture_capture_ = il.klass("UnityEngine", "ScreenCapture");
  capture_method_ = il.typed_method(texture_capture_, "CaptureScreenshotAsTexture", {"System.Int32"});
  LARGE_INTEGER frequency{};
  QueryPerformanceFrequency(&frequency);
  interval_ = (frequency.QuadPart + 29) / 30;
  auto name = L"Local\\AuraResonanceFrame-" + std::to_wstring(client_pid) + L"-" +
              std::wstring(session.begin(), session.end());
  mapping_ = OpenFileMappingW(FILE_MAP_WRITE, FALSE, name.c_str());
  mutex_ = OpenMutexW(SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, (name + L"-lock").c_str());
  if (mapping_ && mutex_)
    view_ = MapViewOfFile(mapping_, FILE_MAP_WRITE, 0, 0, sizeof(Header) + kBytes);
  if (!view_) {
    close();
    throw Error("bridge_capture_mapping", "Cannot open the owner's frame buffer");
  }
  width_ = width;
  height_ = height;
}

void FrameStream::close() {
  if (view_)
    UnmapViewOfFile(view_);
  if (mapping_)
    CloseHandle(mapping_);
  if (mutex_)
    CloseHandle(mutex_);
  view_ = nullptr;
  mapping_ = mutex_ = nullptr;
  generation_ = next_capture_ = error_ = 0;
}

void FrameStream::publish(uint64_t frame, HWND window) {
  if (!enabled())
    return;
  const auto header = static_cast<Header *>(view_);
  LARGE_INTEGER now{};
  QueryPerformanceCounter(&now);
  if (error_) {
    TryLock lock{mutex_};
    if (lock.acquired())
      header->state = error_;
    return;
  }
  if (now.QuadPart < next_capture_)
    return;
  next_capture_ = now.QuadPart + interval_;
  {
    TryLock lock{mutex_};
    if (!lock.acquired()) {
      if (lock.result == WAIT_ABANDONED)
        error_ = kCaptureFailed;
      return;
    }
  }
  try {
    DWORD pid{};
    GetWindowThreadProcessId(window, &pid);
    auto screen = il.klass("UnityEngine", "Screen");
    if (pid != GetCurrentProcessId() || il.scalar<int>(il.stat(screen, "get_width")) != width_ ||
        il.scalar<int>(il.stat(screen, "get_height")) != height_) {
      error_ = kViewportChanged;
      return;
    }
    int scale = 1, mip = 0;
    Texture texture{Root(il.invoke(capture_method_, nullptr, {&scale})), unity_object_};
    auto image = texture.root.get();
    if (!il.alive(image) || il.scalar<int>(il.call(image, "get_width")) != width_ ||
        il.scalar<int>(il.call(image, "get_height")) != height_) {
      error_ = kViewportChanged;
      return;
    }
    Root pixels(il.call(image, "GetPixels32", {&mip}));
    if (!pixels.get() || il.length(pixels.get()) != kBytes / 4) {
      error_ = kCaptureFailed;
      return;
    }
    TryLock lock{mutex_};
    if (lock.acquired()) {
      memcpy(header + 1, il.data(pixels.get()), kBytes);
      *header = {kReady, kWidth, kHeight, uint32_t(kBytes), ++generation_,
                 uint64_t(now.QuadPart), frame};
    } else if (lock.result == WAIT_ABANDONED) {
      error_ = kCaptureFailed;
    }
  } catch (...) {
    error_ = kCaptureFailed;
  }
}
} // namespace bridge
