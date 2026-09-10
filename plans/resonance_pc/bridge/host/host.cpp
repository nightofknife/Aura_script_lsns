#include "runtime.hpp"
#include <bcrypt.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sddl.h>
#include <sstream>
#include <thread>

namespace fs = std::filesystem;
namespace bridge {
namespace {
HMODULE self_module{};
std::atomic<unsigned> clients{0};
std::wstring module_path(HMODULE m) {
  std::wstring p(32768, L'\0');
  auto n = GetModuleFileNameW(m, p.data(), DWORD(p.size()));
  if (!n || n >= p.size())
    throw Error("module_path_invalid", "Cannot resolve module path");
  p.resize(n);
  return p;
}
std::string sha256(const fs::path &p) {
  std::ifstream f(p, std::ios::binary);
  if (!f)
    throw Error("version_mismatch", "Cannot read fingerprint file");
  BCRYPT_ALG_HANDLE alg{};
  BCRYPT_HASH_HANDLE hash{};
  if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0)
    throw Error("hash_failed", "SHA256 provider failed");
  if (BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0) < 0) {
    BCryptCloseAlgorithmProvider(alg, 0);
    throw Error("hash_failed", "SHA256 creation failed");
  }
  char data[65536];
  while (f) {
    f.read(data, sizeof(data));
    auto n = f.gcount();
    if (n && BCryptHashData(hash, reinterpret_cast<PUCHAR>(data), ULONG(n), 0) < 0) {
      BCryptDestroyHash(hash);
      BCryptCloseAlgorithmProvider(alg, 0);
      throw Error("hash_failed", "SHA256 update failed");
    }
  }
  unsigned char digest[32];
  auto status = BCryptFinishHash(hash, digest, 32, 0);
  BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(alg, 0);
  if (status < 0)
    throw Error("hash_failed", "SHA256 final failed");
  std::ostringstream s;
  s << std::hex << std::uppercase << std::setfill('0');
  for (auto b : digest)
    s << std::setw(2) << int(b);
  return s.str();
}
Json profile() {
  auto ga = sha256(module_path(GetModuleHandleW(L"GameAssembly.dll"))),
       up = sha256(module_path(GetModuleHandleW(L"UnityPlayer.dll")));
  fs::path exe = module_path(nullptr);
  auto metadata = exe.parent_path() / (exe.stem().wstring() + L"_Data") / L"il2cpp_data" /
                  L"Metadata" / L"global-metadata.dat";
  auto md = sha256(metadata);
  auto dir = fs::path(module_path(self_module)).parent_path() / L"profiles";
  for (const auto &file : fs::directory_iterator(dir)) {
    if (file.path().extension() != L".json")
      continue;
    std::ifstream input(file.path());
    Json p = Json::parse(input);
    if (p.value("game_assembly_sha256", "") == ga && p.value("unity_player_sha256", "") == up &&
        p.value("metadata_sha256", "") == md)
      return p;
  }
  throw Error("version_mismatch",
              "No profile matches GameAssembly, UnityPlayer and metadata fingerprints");
}
PSECURITY_DESCRIPTOR pipe_security() {
  HANDLE token{};
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token))
    throw Error("pipe_acl_failed", "Cannot open process token");
  DWORD size{};
  GetTokenInformation(token, TokenUser, nullptr, 0, &size);
  std::vector<unsigned char> data(size);
  if (!GetTokenInformation(token, TokenUser, data.data(), size, &size)) {
    CloseHandle(token);
    throw Error("pipe_acl_failed", "Cannot read process identity");
  }
  CloseHandle(token);
  LPWSTR sid{};
  if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER *>(data.data())->User.Sid, &sid))
    throw Error("pipe_acl_failed", "Cannot encode identity");
  std::wstring sddl = L"D:P(A;;GA;;;" + std::wstring(sid) + L")";
  LocalFree(sid);
  PSECURITY_DESCRIPTOR sd{};
  if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &sd,
                                                            nullptr))
    throw Error("pipe_acl_failed", "Cannot build pipe ACL");
  return sd;
}
bool read_line(HANDLE h, std::string &text) {
  char c{};
  DWORD n{};
  while (text.size() < 65536) {
    if (!ReadFile(h, &c, 1, &n, nullptr) || n != 1)
      return false;
    if (c == '\n')
      return true;
    text.push_back(c);
  }
  return false;
}
void serve(HANDLE pipe) {
  FILETIME created{}, exit{}, kernel{}, user{};
  GetProcessTimes(GetCurrentProcess(), &created, &exit, &kernel, &user);
  auto stamp = (uint64_t(created.dwHighDateTime) << 32) | created.dwLowDateTime;
  try {
    std::string line;
    if (read_line(pipe, line)) {
      auto q = Json::parse(line);
      ULONG client{};
      if (!GetNamedPipeClientProcessId(pipe, &client))
        throw Error("session_invalid", "Cannot identify pipe client");
      q["_client_pid"] = client;
      if (q.contains("process_created") && q.at("process_created") != std::to_string(stamp))
        throw Error("process_changed", "Target process creation identity changed");
      auto r = runtime.submit(q);
      r["pid"] = GetCurrentProcessId();
      r["process_created"] = std::to_string(stamp);
      std::string data = r.dump() + "\n";
      size_t offset = 0;
      while (offset < data.size()) {
        DWORD n{};
        if (!WriteFile(pipe, data.data() + offset, DWORD(data.size() - offset), &n, nullptr) || !n)
          break;
        offset += n;
      }
    }
  } catch (const std::exception &e) {
    auto data = Json({{"version", 1},
                      {"pid", GetCurrentProcessId()},
                      {"process_created", std::to_string(stamp)},
                      {"status", "rejected"},
                      {"error", {{"code", "protocol_invalid"}, {"message", e.what()}}}})
                    .dump() +
                "\n";
    DWORD n{};
    WriteFile(pipe, data.data(), DWORD(data.size()), &n, nullptr);
  }
  DisconnectNamedPipe(pipe);
  CloseHandle(pipe);
  clients--;
}
DWORD WINAPI worker(void *) {
  try {
    runtime.initialize(profile());
  } catch (const Error &e) {
    runtime.fail(e.code, e.what());
  } catch (const std::exception &e) {
    runtime.fail("initialization_failed", e.what());
  }
  // The pipe worker must not remain registered with the managed VM while it
  // blocks indefinitely in Win32 IPC; all subsequent Unity work is main-thread.
  il.detach();
  PSECURITY_DESCRIPTOR sd{};
  try {
    sd = pipe_security();
  } catch (...) {
    return 1;
  }
  SECURITY_ATTRIBUTES sa{sizeof(sa), sd, FALSE};
  auto name =
      std::wstring(LR"(\\.\pipe\resonance-input-bridge-)") + std::to_wstring(GetCurrentProcessId());
  for (;;) {
    HANDLE pipe = CreateNamedPipeW(name.c_str(), PIPE_ACCESS_DUPLEX,
                                   PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
                                       PIPE_REJECT_REMOTE_CLIENTS,
                                   8, 65536, 65536, 30000, &sa);
    if (pipe == INVALID_HANDLE_VALUE)
      break;
    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
      CloseHandle(pipe);
      continue;
    }
    if (clients.load() >= 8) {
      DisconnectNamedPipe(pipe);
      CloseHandle(pipe);
      continue;
    }
    clients++;
    std::thread(serve, pipe).detach();
  }
  LocalFree(sd);
  return 0;
}
} // namespace
} // namespace bridge
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    bridge::self_module = module;
    DisableThreadLibraryCalls(module);
    HANDLE thread = CreateThread(nullptr, 0, bridge::worker, nullptr, 0, nullptr);
    if (thread)
      CloseHandle(thread);
  } else if (reason == DLL_PROCESS_DETACH) {
    bridge::process_exiting = true;
  }
  return TRUE;
}
