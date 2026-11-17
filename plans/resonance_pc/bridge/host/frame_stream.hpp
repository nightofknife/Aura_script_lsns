#pragma once
#include "il2cpp.hpp"

namespace bridge {
class FrameStream {
public:
  void open(const std::string &session, DWORD client_pid, int width, int height);
  void close();
  bool enabled() const { return view_ != nullptr; }
  void publish(uint64_t frame, HWND window);

private:
  HANDLE mapping_{}, mutex_{};
  void *view_{};
  int width_{}, height_{};
  uint64_t generation_{};
  int64_t interval_{}, next_capture_{};
  uint32_t error_{};
  Class texture_capture_{}, unity_object_{};
  Method capture_method_{};
};
} // namespace bridge
