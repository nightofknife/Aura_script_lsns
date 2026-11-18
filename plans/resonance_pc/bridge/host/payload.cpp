#include "runtime.hpp"

namespace bridge {
namespace {
Runtime &runtime(void *object) { return *static_cast<Runtime *>(object); }

void *create(const char *profile, bool capture, WriteResponse report, void *context) noexcept {
  try {
    auto host = std::make_unique<Runtime>();
    host->initialize(Json::parse(profile), capture);
    return host.release();
  } catch (const std::exception &e) {
    report(context, e.what(), uint32_t(std::char_traits<char>::length(e.what())));
    return nullptr;
  }
}

void request(void *object, const char *payload, WriteResponse write, void *context) noexcept {
  try {
    Json q = Json::parse(payload);
    Json reply;
    try {
      reply = runtime(object).submit(q);
    } catch (const std::exception &e) {
      reply = {{"version", 1}, {"sequence", q.value("sequence", uint64_t(0))},
               {"session", q.value("session", "")}, {"status", "rejected"},
               {"error", {{"code", "protocol_invalid"}, {"message", e.what()}}}};
    }
    auto data = reply.dump();
    write(context, data.data(), uint32_t(data.size()));
  } catch (...) {
    // A missing response is treated as an unknown result by the bootstrap.
  }
}

const HostApi api{
    sizeof(HostApi), host_abi,
    create,
    [](void *object) noexcept { delete &runtime(object); },
    request,
    [](void *object) noexcept { return runtime(object).idle(); },
    [](void *object, float *values) noexcept {
      auto &host = runtime(object);
      if (!host.owns())
        return false;
      const auto p = host.position();
      values[0] = p.x;
      values[1] = p.y;
      values[2] = p.z;
      return true;
    },
    [](void *object, float *values) noexcept {
      auto &host = runtime(object);
      if (!host.owns())
        return false;
      const auto p = host.wheel();
      values[0] = p.x;
      values[1] = p.y;
      return true;
    },
    [](void *object, int key, int phase, bool *pressed) noexcept {
      auto &host = runtime(object);
      if (!host.owns())
        return false;
      *pressed = host.button(key, phase);
      return true;
    },
    [](void *object) noexcept { return runtime(object).owns(); },
    [](void *object, Obj es, Method method, EventUpdateFn original) noexcept {
      runtime(object).event_update(es, method, original);
    },
    [](void *object) noexcept { runtime(object).publish(); },
    [](void *object) noexcept { runtime(object).complete(); },
    [](void *object) noexcept { runtime(object).capture_frame(); }};
} // namespace
} // namespace bridge

extern "C" __declspec(dllexport) const bridge::HostApi *AuraBridgeGetHostApi(uint32_t abi) {
  return abi == bridge::host_abi ? &bridge::api : nullptr;
}
