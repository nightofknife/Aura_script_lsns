#pragma once

#include "host_api.hpp"
#include <filesystem>
#include <nlohmann/json.hpp>

namespace bridge {
using Json = nlohmann::json;

// Acquiring the function table and counting this call share the unload gate.
// The reference lasts until execution has returned to the bootstrap.
class HostCall {
public:
  explicit HostCall(bool rpc = false);
  ~HostCall();
  HostCall(const HostCall &) = delete;
  HostCall &operator=(const HostCall &) = delete;
  explicit operator bool() const { return api != nullptr; }

  const HostApi *api{};
  void *object{};
  uint64_t instance{};

private:
  bool rpc_;
};

std::string sha256(const std::filesystem::path &path);
void configure_host_cache(std::filesystem::path root, const Json &profile);
Json bridge_request(const Json &request);
void maintain_host();
} // namespace bridge
