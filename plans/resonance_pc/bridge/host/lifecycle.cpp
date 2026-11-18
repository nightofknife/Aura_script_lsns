#include "lifecycle.hpp"
#include "il2cpp.hpp"
#include "unity_hooks.hpp"
#include <mutex>

namespace fs = std::filesystem;
namespace bridge {
namespace {
constexpr uint64_t idle_ms = 30000, drain_timeout_ms = 3000;
enum class State { unloaded, loading, loaded, draining, unloading, unload_failed };

struct Lifecycle {
  std::mutex mutex;
  State state = State::unloaded;
  fs::path cache, path;
  std::string profile, hash, error;
  HMODULE module{};
  const HostApi *api{};
  void *object{};
  uint64_t instance{}, last_used{}, drain_started{};
  unsigned calls{}, rpc_calls{};

  void begin_drain() {
    state = State::draining;
    drain_started = GetTickCount64();
  }

  bool idle() const { return !rpc_calls && api->idle(object); }

  Json status() const {
    const char *phase = "unloaded";
    switch (state) {
    case State::unloaded: break;
    case State::loading: phase = "loading"; break;
    case State::loaded: phase = "loaded"; break;
    case State::draining: phase = "draining"; break;
    case State::unloading: phase = "unloading"; break;
    case State::unload_failed: phase = "unload_failed"; break;
    }
    return {{"bootstrap_abi", bootstrap_abi}, {"host_abi", host_abi},
            {"host_state", phase}, {"host_hash", hash}, {"host_instance", instance},
            {"cache_root", cache.u8string()}, {"in_flight_calls", calls}, {"lifecycle_error", error}};
  }
};

// Unity and the pipe server can use the bootstrap until process exit.
Lifecycle &lifecycle = *new Lifecycle;

void copy_response(void *context, const char *data, uint32_t size) noexcept {
  try {
    static_cast<std::string *>(context)->assign(data, size);
  } catch (...) {
  }
}

Json completed(Json result) { return {{"status", "completed"}, {"result", std::move(result)}}; }
Json rejected(const char *code, const std::string &message) {
  return {{"status", "rejected"}, {"error", {{"code", code}, {"message", message}}}};
}

bool checksummed_name(const std::string &hash) {
  return hash.size() == 64 && hash.find_first_not_of("0123456789ABCDEF") == std::string::npos;
}

Json prepare(const std::string &hash) {
  if (!checksummed_name(hash))
    return rejected("protocol_invalid", "A SHA256 of the staged bridge DLL is required");
  if (auto error = unity_hook_error(); !error.empty())
    return rejected("bridge_failed", error);
  if (!unity_hooks_ready())
    return rejected("bootstrap_pending", "Unity bridge callbacks are starting up");

  auto &host = lifecycle;
  std::unique_lock<std::mutex> lock(host.mutex);
  if (host.state == State::loaded) {
    if (host.hash == hash) {
      host.last_used = GetTickCount64();
      return completed(host.status());
    }
    if (!host.idle())
      return rejected("input_session_busy", "The loaded bridge still owns a session");
    host.begin_drain();
  }
  if (host.state == State::unload_failed)
    return rejected("bridge_unload_failed", host.error);
  if (host.state != State::unloaded)
    return rejected("bridge_transition", "The bridge is loading or unloading");

  auto path = host.cache / L"host" / hash / L"resonance_bridge_host.dll";
  host.state = State::loading;
  host.error.clear();
  lock.unlock();

  HMODULE module{};
  const HostApi *api{};
  void *object{};
  std::string error;
  try {
    if (sha256(path) != hash)
      throw Error("bridge_hash_mismatch", "Staged bridge DLL checksum does not match");
    module = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                                LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!module)
      throw Error("bridge_load_failed", "LoadLibraryEx failed: " + std::to_string(GetLastError()));
    auto get_api = reinterpret_cast<GetHostApi>(GetProcAddress(module, "AuraBridgeGetHostApi"));
    api = get_api ? get_api(host_abi) : nullptr;
    if (!api || api->abi != host_abi || api->size != sizeof(HostApi))
      throw Error("bridge_abi_mismatch", "The bridge DLL requires a compatible bootstrap");
    object = api->create(host.profile.c_str(), unity_capture_available(), copy_response, &error);
    if (!object)
      throw Error("bridge_load_failed", error.empty() ? "Bridge initialization failed" : error);
  } catch (const std::exception &e) {
    error = e.what();
  }
  if (!object && module) {
    FreeLibrary(module);
    if (GetModuleHandleW(path.c_str())) {
      lock.lock();
      host.path = path;
      host.hash = hash;
      host.state = State::unload_failed;
      host.error = "Rejected bridge DLL remained loaded: " + error;
      return rejected("bridge_unload_failed", host.error);
    }
  }

  lock.lock();
  if (!object) {
    host.state = State::unloaded;
    host.error = error;
    return rejected("bridge_load_failed", error);
  }
  host.module = module;
  host.api = api;
  host.object = object;
  host.path = std::move(path);
  host.hash = hash;
  ++host.instance;
  host.last_used = GetTickCount64();
  host.state = State::loaded;
  return completed(host.status());
}
} // namespace

HostCall::HostCall(bool rpc) : rpc_(rpc) {
  auto &host = lifecycle;
  std::lock_guard<std::mutex> lock(host.mutex);
  if (host.state != State::loaded)
    return;
  ++host.calls;
  if (rpc_)
    ++host.rpc_calls;
  api = host.api;
  object = host.object;
  instance = host.instance;
}

HostCall::~HostCall() {
  if (!api)
    return;
  auto &host = lifecycle;
  std::lock_guard<std::mutex> lock(host.mutex);
  --host.calls;
  if (rpc_) {
    --host.rpc_calls;
    host.last_used = GetTickCount64();
  }
}

void configure_host_cache(fs::path root, const Json &profile) {
  lifecycle.cache = std::move(root);
  lifecycle.profile = profile.dump();
}

void maintain_host() {
  auto &host = lifecycle;
  std::unique_lock<std::mutex> lock(host.mutex);
  auto now = GetTickCount64();
  if (host.state == State::loaded && now - host.last_used >= idle_ms && host.idle())
    host.begin_drain();
  if (host.state != State::draining)
    return;
  if (host.calls) {
    if (now >= host.drain_started && now - host.drain_started >= drain_timeout_ms)
      host.error = "Waiting for callbacks to return to the bootstrap";
    return;
  }

  // A closed session has already released the managed resources on Unity's
  // main thread. The gate now prevents any thread from entering this DLL.
  host.state = State::unloading;
  auto api = host.api;
  auto object = host.object;
  auto module = host.module;
  const auto path = host.path;
  lock.unlock();
  api->destroy(object);
  bool freed = FreeLibrary(module) != FALSE && !GetModuleHandleW(path.c_str());
  lock.lock();
  host.api = nullptr;
  host.object = nullptr;
  host.module = nullptr;
  host.state = freed ? State::unloaded : State::unload_failed;
  if (freed) {
    host.path.clear();
    host.hash.clear();
    host.error.clear();
  } else {
    host.error = "The old bridge DLL remained mapped after release";
  }
}

Json bridge_request(const Json &request) {
  const auto op = request.value("op", "");
  if (request.value("version", 0) != 1 || request.value("session", "").empty() ||
      !request.value("sequence", uint64_t(0)))
    return rejected("protocol_invalid", "Version, session and sequence are required");
  if (op == "prepare_host")
    return prepare(request.at("args").at("host_hash").get<std::string>());
  if (op == "unload_host") {
    auto &host = lifecycle;
    std::lock_guard<std::mutex> lock(host.mutex);
    if (request.at("args").value("host_instance", uint64_t(0)) != host.instance)
      return rejected("session_invalid", "The bridge instance changed");
    if (host.state == State::loaded) {
      if (!host.idle())
        return rejected("input_session_busy", "The bridge still owns a session");
      host.begin_drain();
    }
    if (host.state == State::unload_failed)
      return rejected("bridge_unload_failed", host.error);
    return completed(host.status());
  }

  HostCall call(op != "status");
  if (op == "status") {
    std::string response;
    if (call)
      call.api->request(call.object, request.dump().c_str(), copy_response, &response);
    auto hook_error = unity_hook_error();
    auto result = response.empty()
                      ? Json{{"state", hook_error.empty() ? "bootstrap_pending" : "failed"},
                             {"capabilities", Json::object()}, {"error", std::move(hook_error)}}
                      : Json::parse(response).at("result");
    std::lock_guard<std::mutex> lock(lifecycle.mutex);
    result.update(lifecycle.status());
    return completed(std::move(result));
  }
  if (!call || request.value("host_instance", uint64_t(0)) != call.instance)
    return rejected("session_invalid", "The bridge instance changed");

  std::string response;
  call.api->request(call.object, request.dump().c_str(), copy_response, &response);
  return response.empty() ? Json{{"status", "unknown"}} : Json::parse(response);
}
} // namespace bridge
