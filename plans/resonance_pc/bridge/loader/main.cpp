#include <windows.h>

#include <filesystem>
#include <iostream>
#include <string>
#include <tlhelp32.h>

namespace {
struct Handle {
  HANDLE value{};
  ~Handle() {
    if (value && value != INVALID_HANDLE_VALUE)
      CloseHandle(value);
  }
};
uintptr_t remote_module(DWORD pid, const wchar_t *name) {
  Handle snap{CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)};
  MODULEENTRY32W e{sizeof(e)};
  if (Module32FirstW(snap.value, &e))
    do {
      if (_wcsicmp(e.szModule, name) == 0)
        return reinterpret_cast<uintptr_t>(e.modBaseAddr);
    } while (Module32NextW(snap.value, &e));
  return 0;
}
} // namespace
int wmain(int argc, wchar_t **argv) {
  try {
    DWORD pid{};
    std::filesystem::path dll;
    for (int i = 1; i < argc; i++) {
      std::wstring a = argv[i];
      if (a == L"--pid" && i + 1 < argc)
        pid = std::stoul(argv[++i]);
      else if (a == L"--dll" && i + 1 < argc)
        dll = argv[++i];
      else
        throw std::runtime_error("Usage: resonance_bridge_loader --pid PID --dll ABSOLUTE_DLL");
    }
    if (!pid || dll.empty() || !dll.is_absolute() || !std::filesystem::is_regular_file(dll))
      throw std::runtime_error("PID and existing absolute DLL path required");
    if (!remote_module(pid, L"GameAssembly.dll") || !remote_module(pid, L"UnityPlayer.dll"))
      throw std::runtime_error("Target is not the expected IL2CPP process");
    if (remote_module(pid, dll.filename().c_str())) {
      std::cout << "already_loaded\n";
      return 0;
    }
    Handle process{OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                                   PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ,
                               FALSE, pid)};
    if (!process.value)
      throw std::runtime_error("OpenProcess failed");
    BOOL wow{};
    if (!IsWow64Process(process.value, &wow) || wow)
      throw std::runtime_error("Target must be x64");
    auto path = dll.wstring();
    size_t bytes = (path.size() + 1) * sizeof(wchar_t);
    void *remote =
        VirtualAllocEx(process.value, nullptr, bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!remote)
      throw std::runtime_error("VirtualAllocEx failed");
    if (!WriteProcessMemory(process.value, remote, path.c_str(), bytes, nullptr)) {
      VirtualFreeEx(process.value, remote, 0, MEM_RELEASE);
      throw std::runtime_error("WriteProcessMemory failed");
    }
    auto load = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    MEMORY_BASIC_INFORMATION info{};
    VirtualQuery(reinterpret_cast<void *>(load), &info, sizeof(info));
    wchar_t owner[32768];
    GetModuleFileNameW(static_cast<HMODULE>(info.AllocationBase), owner, 32768);
    auto base = remote_module(pid, std::filesystem::path(owner).filename().c_str());
    if (!base) {
      VirtualFreeEx(process.value, remote, 0, MEM_RELEASE);
      throw std::runtime_error("Remote LoadLibrary owner module missing");
    }
    auto entry =
        reinterpret_cast<LPTHREAD_START_ROUTINE>(base + reinterpret_cast<uintptr_t>(load) -
                                                 reinterpret_cast<uintptr_t>(info.AllocationBase));
    Handle thread{CreateRemoteThread(process.value, nullptr, 0, entry, remote, 0, nullptr)};
    if (!thread.value) {
      VirtualFreeEx(process.value, remote, 0, MEM_RELEASE);
      throw std::runtime_error("CreateRemoteThread failed");
    }
    auto wait = WaitForSingleObject(thread.value, 30000);
    if (wait != WAIT_OBJECT_0)
      throw std::runtime_error("Loader completion unknown; buffer retained until process exit");
    VirtualFreeEx(process.value, remote, 0, MEM_RELEASE);
    if (!remote_module(pid, dll.filename().c_str()))
      throw std::runtime_error("LoadLibrary did not load bridge (check DLL dependencies)");
    std::cout << "loaded\n";
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what() << " (Win32 " << GetLastError() << ")\n";
    return 1;
  }
}
