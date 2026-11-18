#include "lifecycle.hpp"
#include "unity_hooks.hpp"
#include "il2cpp.hpp"
#include <MinHook.h>
extern "C" {
#include <hde64.h>
}
#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>

namespace bridge {
namespace {
using V3Fn = void (*)(Vec3 *);
using V2Fn = void (*)(Vec2 *);
using IntFn = bool (*)(int);
using StringFn = bool (*)(Obj);
using FloatFn = float (*)(Obj);
using BoolFn = bool (*)();
V3Fn original_position{};
V2Fn original_wheel{};
IntFn original_mouse[3]{}, original_key[3]{};
StringFn original_key_string[3]{}, original_named[3]{};
FloatFn original_axis[2]{};
BoolFn original_present{}, original_any[2]{};
std::vector<void *> hooks;
EventUpdateFn original_event_update{};
Root loop_root;
std::atomic<bool> hooks_ready{}, capture_available{}, hooks_failed{};
bool install_started{};
std::mutex hook_error_mutex;
std::string hook_error;
void install_loop();
void fail_hooks(const std::string &reason) {
  std::lock_guard<std::mutex> lock(hook_error_mutex);
  hook_error = reason;
  hooks_failed = true;
}
bool equal_string(Obj o, const wchar_t *s) {
  if (!o)
    return false;
  int n = *reinterpret_cast<int *>(static_cast<char *>(o) + 16);
  return n == int(wcslen(s)) && memcmp(static_cast<char *>(o) + 20, s, n * 2) == 0;
}
int key_string(Obj o) {
  if (equal_string(o, L"mouse 0"))
    return 0;
  if (equal_string(o, L"mouse 1"))
    return 1;
  if (equal_string(o, L"mouse 2"))
    return 2;
  return -1;
}
int named_button(Obj o) {
  if (equal_string(o, L"Fire1"))
    return 0;
  if (equal_string(o, L"Fire2"))
    return 1;
  if (equal_string(o, L"Fire3"))
    return 2;
  return -1;
}
void position_hook(Vec3 *out) {
  HostCall host;
  float position[3];
  if (host && host.api->position(host.object, position))
    *out = {position[0], position[1], position[2]};
  else
    original_position(out);
}
void wheel_hook(Vec2 *out) {
  HostCall host;
  float wheel[2];
  if (host && host.api->wheel(host.object, wheel))
    *out = {wheel[0], wheel[1]};
  else
    original_wheel(out);
}
bool read_button(int key, int phase, bool &value) {
  HostCall host;
  return host && host.api->button(host.object, key, phase, &value);
}
template <int P> bool mouse_hook(int k) {
  bool value{};
  return read_button(k, P, value) ? value : original_mouse[P](k);
}
template <int P> bool key_hook(int k) {
  bool value{};
  return read_button(k - 323, P, value) ? value : original_key[P](k);
}
template <int P> bool string_hook(Obj k) {
  bool value{};
  return read_button(key_string(k), P, value) ? value : original_key_string[P](k);
}
template <int P> bool named_hook(Obj k) {
  bool value{};
  return read_button(named_button(k), P, value) ? value : original_named[P](k);
}
template <int P> float axis_hook(Obj k) {
  HostCall host;
  return host && host.api->owns_input(host.object) ? 0.0f : original_axis[P](k);
}
bool present_hook() {
  HostCall host;
  return (host && host.api->owns_input(host.object)) || original_present();
}
template <int P> bool any_hook() {
  HostCall host;
  bool pressed{};
  if (!host || !host.api->button(host.object, 0, P, &pressed))
    return original_any[P]();
  if (pressed)
    return true;
  for (int key = 1; key <= 2; ++key)
    if (host.api->button(host.object, key, P, &pressed) && pressed)
      return true;
  return false;
}
void event_hook(Obj es, Method method) {
  if (!install_started) {
    install_started = true;
    try {
      install_loop();
      hooks_ready = true;
    } catch (const std::exception &e) {
      fail_hooks(e.what());
    }
  }
  HostCall host;
  if (host)
    host.api->event_update(host.object, es, method, original_event_update);
  else
    original_event_update(es, method);
}
void publish_callback() {
  if (HostCall host; host)
    host.api->publish(host.object);
}
void complete_callback() {
  if (HostCall host; host)
    host.api->complete(host.object);
}
void capture_callback() {
  if (HostCall host; host)
    host.api->capture(host.object);
}
using LoopFn = void (*)();
LoopFn publish_slot = publish_callback, complete_slot = complete_callback;
LoopFn capture_slot = capture_callback;
template <class T> void hook(void *address, void *replacement, T &original) {
  if (std::find(hooks.begin(), hooks.end(), address) != hooks.end())
    throw Error("version_mismatch", "Unexpected input ABI target alias");
  auto status = MH_CreateHook(address, replacement, reinterpret_cast<void **>(&original));
  if (status != MH_OK)
    throw Error("hook_install_failed", MH_StatusToString(status));
  hooks.push_back(address);
}
template <class T> void ihook(const char *name, void *replacement, T &original) {
  auto p = il.icall((std::string("UnityEngine.Input::") + name).c_str());
  MEMORY_BASIC_INFORMATION info{};
  VirtualQuery(p, &info, sizeof(info));
  if (info.AllocationBase != GetModuleHandleW(L"UnityPlayer.dll"))
    throw Error("version_mismatch", "Input icall does not belong to UnityPlayer");
  // Decode whole instructions, never scan arbitrary bytes as instructions. A
  // populated wrapper cache must agree with resolve_icall; cold slots may be 0.
  auto check_cache = [&](const char *wrapper, int argc) {
    auto code = static_cast<unsigned char *>(
        il.pointer(il.method(il.klass("UnityEngine", "Input"), wrapper, argc)));
    bool found = false;
    for (size_t offset = 0; offset < 96;) {
      hde64s ins{};
      auto length = hde64_disasm(code + offset, &ins);
      if (!length || (ins.flags & F_ERROR))
        break;
      if (ins.opcode == 0x8B && ins.rex_w && ins.modrm_mod == 0 && ins.modrm_rm == 5) {
        int32_t displacement{};
        memcpy(&displacement, code + offset + length - 4, 4);
        void *slot = code + offset + length + displacement;
        MEMORY_BASIC_INFORMATION slot_info{};
        VirtualQuery(slot, &slot_info, sizeof(slot_info));
        if (slot_info.AllocationBase == il.module) {
          void *cached = *static_cast<void **>(slot);
          MEMORY_BASIC_INFORMATION cached_info{};
          if (cached)
            VirtualQuery(cached, &cached_info, sizeof(cached_info));
          if (!cached || cached_info.AllocationBase == GetModuleHandleW(L"UnityPlayer.dll")) {
            found = true;
            if (cached && cached != p)
              throw Error("version_mismatch",
                          std::string("Input cache differs from resolver: ") + wrapper);
            break;
          }
        }
      }
      if (ins.opcode == 0xC3)
        break;
      offset += length;
    }
    if (!found)
      throw Error("version_mismatch",
                  std::string("Cannot validate Input wrapper cache: ") + wrapper);
  };
  int argc = std::strncmp(name, "get_", 4) == 0 ? 0 : 1;
  if (std::strstr(name, "_Injected"))
    argc = 1;
  check_cache(name, argc);
  if (std::strcmp(name, "get_mousePosition_Injected") == 0)
    check_cache("get_mousePosition", 0);
  if (std::strcmp(name, "get_mouseScrollDelta_Injected") == 0)
    check_cache("get_mouseScrollDelta", 0);
  hook(p, replacement, original);
}
void install_input_hooks() {
  ihook("get_mousePosition_Injected", reinterpret_cast<void *>(position_hook), original_position);
  ihook("get_mouseScrollDelta_Injected", reinterpret_cast<void *>(wheel_hook), original_wheel);
  ihook("GetMouseButton", reinterpret_cast<void *>(mouse_hook<0>), original_mouse[0]);
  ihook("GetMouseButtonDown", reinterpret_cast<void *>(mouse_hook<1>), original_mouse[1]);
  ihook("GetMouseButtonUp", reinterpret_cast<void *>(mouse_hook<2>), original_mouse[2]);
  ihook("GetKeyInt", reinterpret_cast<void *>(key_hook<0>), original_key[0]);
  ihook("GetKeyDownInt", reinterpret_cast<void *>(key_hook<1>), original_key[1]);
  ihook("GetKeyUpInt", reinterpret_cast<void *>(key_hook<2>), original_key[2]);
  ihook("GetKeyString", reinterpret_cast<void *>(string_hook<0>), original_key_string[0]);
  ihook("GetKeyDownString", reinterpret_cast<void *>(string_hook<1>), original_key_string[1]);
  ihook("GetKeyUpString", reinterpret_cast<void *>(string_hook<2>), original_key_string[2]);
  ihook("GetButton", reinterpret_cast<void *>(named_hook<0>), original_named[0]);
  ihook("GetButtonDown", reinterpret_cast<void *>(named_hook<1>), original_named[1]);
  ihook("GetButtonUp", reinterpret_cast<void *>(named_hook<2>), original_named[2]);
  ihook("GetAxis", reinterpret_cast<void *>(axis_hook<0>), original_axis[0]);
  ihook("GetAxisRaw", reinterpret_cast<void *>(axis_hook<1>), original_axis[1]);
  ihook("get_mousePresent", reinterpret_cast<void *>(present_hook), original_present);
  ihook("get_anyKey", reinterpret_cast<void *>(any_hook<0>), original_any[0]);
  ihook("get_anyKeyDown", reinterpret_cast<void *>(any_hook<1>), original_any[1]);
  for (auto p : hooks) {
    auto s = MH_EnableHook(p);
    if (s != MH_OK && s != MH_ERROR_ENABLED)
      throw Error("hook_install_failed", MH_StatusToString(s));
  }
}
void install_loop() {
  auto loop = il.klass("UnityEngine.LowLevel", "PlayerLoop"),
       node = il.klass("UnityEngine.LowLevel", "PlayerLoopSystemInternal");
  if (il.value_size(node) != 40)
    throw Error("version_mismatch", "Unexpected PlayerLoopSystemInternal ABI");
  struct Node {
    Obj type, delegate;
    void *function;
    void *condition;
    int count;
    int pad;
  };
  static_assert(sizeof(Node) == 40);
  Root array(il.stat(loop, "GetCurrentPlayerLoopInternal"));
  auto n = il.length(array.get());
  if (n < 2 || n > 2048)
    throw Error("loop_layout_unsupported", "Invalid PlayerLoop size");
  auto begin = static_cast<Node *>(il.data(array.get()));
  std::vector<Node> nodes(begin, begin + n);
  size_t input = n, frame_end = n;
  for (size_t i = 0; i < n; i++)
    if (nodes[i].type) {
      auto name = il.utf8(il.call(nodes[i].type, "get_FullName"));
      if (name == "UnityEngine.PlayerLoop.EarlyUpdate+UpdateInputManager")
        input = i;
      if (name == "UnityEngine.PlayerLoop.PostLateUpdate+TriggerEndOfFrameCallbacks")
        frame_end = i;
    }
  if (input == n || nodes[input].count != 0)
    throw Error("loop_layout_unsupported", "UpdateInputManager leaf missing");
  std::vector<size_t> parents(n, n), stack;
  if (nodes[0].count != int(n - 1))
    throw Error("loop_layout_unsupported", "Root descendant span differs from supported ABI");
  for (size_t i = 0; i < n; i++) {
    while (!stack.empty() && stack.back() + size_t(nodes[stack.back()].count) < i)
      stack.pop_back();
    if (nodes[i].count < 0 || i + size_t(nodes[i].count) >= n)
      throw Error("loop_layout_unsupported", "Invalid PlayerLoop subtree span");
    if (!stack.empty()) {
      parents[i] = stack.back();
      if (i + size_t(nodes[i].count) > stack.back() + size_t(nodes[stack.back()].count))
        throw Error("loop_layout_unsupported", "Crossing PlayerLoop subtree spans");
    }
    if (nodes[i].count)
      stack.push_back(i);
  }
  if (parents[input] == n)
    throw Error("loop_layout_unsupported", "Input has no parent");
  if (frame_end < n && frame_end > input && !nodes[frame_end].count && parents[frame_end] < n) {
    for (size_t p = parents[frame_end]; p < n; p = parents[p])
      nodes[p].count++;
    nodes.insert(nodes.begin() + frame_end + 1, Node{nullptr, nullptr, &capture_slot, nullptr, 0, 0});
    capture_available = true;
  }
  const size_t insert = input + 1;
  for (size_t p = parents[input]; p < n; p = parents[p])
    nodes[p].count++;
  nodes.insert(nodes.begin() + insert, Node{nullptr, nullptr, &publish_slot, nullptr, 0, 0});
  nodes[0].count++;
  nodes.push_back(Node{nullptr, nullptr, &complete_slot, nullptr, 0, 0});
  Root replacement(il.array(node, nodes.size()));
  memcpy(il.data(replacement.get()), nodes.data(), nodes.size() * sizeof(Node));
  il.stat(loop, "SetPlayerLoopInternal", {replacement.get()});
  loop_root = std::move(replacement);
  install_input_hooks();
}
} // namespace

void start_unity_hooks() {
  il.load();
  il.attach();
  try {
    auto status = MH_Initialize();
    if (status != MH_OK)
      throw Error("hook_install_failed", MH_StatusToString(status));
    auto es = il.klass("UnityEngine.EventSystems", "EventSystem");
    auto address = il.pointer(il.method(es, "Update", 0));
    hook(address, reinterpret_cast<void *>(event_hook), original_event_update);
    status = MH_EnableHook(address);
    if (status != MH_OK)
      throw Error("hook_install_failed", MH_StatusToString(status));
  } catch (...) {
    il.detach();
    throw;
  }
  il.detach();
}
bool unity_hooks_ready() { return hooks_ready; }
bool unity_capture_available() { return capture_available; }
std::string unity_hook_error() {
  if (!hooks_failed)
    return {};
  std::lock_guard<std::mutex> lock(hook_error_mutex);
  return hook_error;
}
} // namespace bridge
