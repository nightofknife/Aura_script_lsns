#pragma once

#include <cstdint>

namespace bridge {
inline constexpr uint32_t host_abi = 1;
inline constexpr uint32_t bootstrap_abi = 1;
using EventUpdateFn = void (*)(void *, const void *);
using WriteResponse = void (*)(void *, const char *, uint32_t) noexcept;

// All allocations on either side of this interface are freed on that same side.
// A response is written before request() returns. No callbacks outlive their call.
struct HostApi {
  uint32_t size, abi;
  void *(*create)(const char *profile, bool capture, WriteResponse, void *) noexcept;
  void (*destroy)(void *) noexcept;
  void (*request)(void *, const char *, WriteResponse, void *) noexcept;
  bool (*idle)(void *) noexcept;
  bool (*position)(void *, float *) noexcept;
  bool (*wheel)(void *, float *) noexcept;
  bool (*button)(void *, int key, int phase, bool *pressed) noexcept;
  bool (*owns_input)(void *) noexcept;
  void (*event_update)(void *, void *, const void *, EventUpdateFn) noexcept;
  void (*publish)(void *) noexcept;
  void (*complete)(void *) noexcept;
  void (*capture)(void *) noexcept;
};

using GetHostApi = const HostApi *(*)(uint32_t);
} // namespace bridge
