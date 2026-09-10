#include "runtime.hpp"
#include <MinHook.h>
extern "C" {
#include <hde64.h>
}
#include <algorithm>
#include <cmath>
#include <cstring>
#include <functional>
#include <set>

namespace bridge {
// Intentionally process-lifetime: static destruction runs under loader lock and
// after Unity may have torn down GC. Session resources are explicitly released
// on the live main thread; this inert host is never force-unloaded.
Runtime &runtime = *new Runtime;
EventUpdateFn original_event_update{};
namespace {
uint64_t now() { return GetTickCount64(); }
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
  if (runtime.owns())
    *out = runtime.position();
  else
    original_position(out);
}
void wheel_hook(Vec2 *out) {
  if (runtime.owns())
    *out = runtime.wheel();
  else
    original_wheel(out);
}
template <int P> bool mouse_hook(int k) {
  return runtime.owns() ? runtime.button(k, P) : original_mouse[P](k);
}
template <int P> bool key_hook(int k) {
  return runtime.owns() ? runtime.button(k - 323, P) : original_key[P](k);
}
template <int P> bool string_hook(Obj k) {
  return runtime.owns() ? runtime.button(key_string(k), P) : original_key_string[P](k);
}
template <int P> bool named_hook(Obj k) {
  return runtime.owns() ? runtime.button(named_button(k), P) : original_named[P](k);
}
template <int P> float axis_hook(Obj k) { return runtime.owns() ? 0.0f : original_axis[P](k); }
bool present_hook() { return runtime.owns() ? true : original_present(); }
template <int P> bool any_hook() {
  return runtime.owns() ? (runtime.button(0, P) || runtime.button(1, P) || runtime.button(2, P))
                        : original_any[P]();
}
void event_hook(Obj es, Method m) { runtime.event_update(es, m); }
void publish_callback() { runtime.publish(); }
void complete_callback() { runtime.complete(); }
using LoopFn = void (*)();
LoopFn publish_slot = publish_callback, complete_slot = complete_callback;
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
Obj game_object(Obj o) { return il.call(o, "get_gameObject"); }
Obj transform(Obj o) { return il.call(o, "get_transform"); }
Obj parent(Obj t) { return il.call(t, "get_parent"); }
Obj component(Obj go, Class type) {
  return il.invoke(il.typed_method(il.object_class(go), "GetComponent", {"System.Type"}), go,
                   {il.type(type)});
}
bool active(Obj c) { return il.alive(c) && il.scalar<bool>(il.call(c, "get_isActiveAndEnabled")); }
int active_scene_handle() {
  Root scene(il.stat(il.klass("UnityEngine.SceneManagement", "SceneManager"), "GetActiveScene"));
  return il.get<int>(scene.get(), "m_Handle");
}
// The active UI branch is the first transform beneath the nearest Canvas. This
// permits sibling viewports without crossing an overlay panel boundary.
Obj branch(Obj go) {
  Obj t = transform(go), previous = t;
  auto canvas = il.klass("UnityEngine", "Canvas");
  for (int i = 0; t && i < 128; i++) {
    if (component(game_object(t), canvas))
      return previous;
    previous = t;
    t = parent(t);
  }
  return nullptr;
}
bool contains(Obj rect, Vec2 p, Obj cam) {
  return il.scalar<bool>(il.stat(il.klass("UnityEngine", "RectTransformUtility"),
                                 "RectangleContainsScreenPoint", {rect, &p, cam}));
}
void set_prop(Obj o, const char *name, void *value) { il.call(o, name, {value}); }
double number(const Json &a, const char *key, double fallback = 0) {
  double n = a.value(key, fallback);
  if (!std::isfinite(n))
    throw Error("input_parameter_invalid", key);
  return n;
}
double ties_even(double v) {
  double lo = std::floor(v), fraction = v - lo;
  if (fraction < .5)
    return lo;
  if (fraction > .5)
    return lo + 1;
  return std::fmod(lo, 2) == 0 ? lo : lo + 1;
}
} // namespace

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
void disable_input_hooks() {
  for (auto p : hooks)
    MH_DisableHook(p);
}

Vec3 Runtime::position() const {
  std::lock_guard<std::mutex> l(snapshot_mutex_);
  return snapshot_.position;
}
Vec2 Runtime::wheel() const {
  std::lock_guard<std::mutex> l(snapshot_mutex_);
  return snapshot_.wheel;
}
bool Runtime::button(int k, int phase) const {
  if (k < 0 || k > 2)
    return false;
  std::lock_guard<std::mutex> l(snapshot_mutex_);
  auto mask = phase == 0 ? snapshot_.held : phase == 1 ? snapshot_.down : snapshot_.up;
  return (mask & (1 << k)) != 0;
}
void Runtime::set_position(Vec2 p) {
  std::lock_guard<std::mutex> l(snapshot_mutex_);
  snapshot_.position = {p.x, p.y, 0};
}
void Runtime::initialize(const Json &profile) {
  profile_ = profile;
  il.load();
  il.attach();
  capabilities_ = {{"absolute_pointer", false}, {"direct_drag", false}, {"scroll", false},
                   {"relative_look", false},    {"keyboard", false},    {"text_input", false},
                   {"background_input", false}};
  auto s = MH_Initialize();
  if (s != MH_OK)
    throw Error("hook_install_failed", MH_StatusToString(s));
  auto es = il.klass("UnityEngine.EventSystems", "EventSystem");
  auto m = il.method(es, "Update", 0);
  hook(il.pointer(m), reinterpret_cast<void *>(event_hook), original_event_update);
  auto status = MH_EnableHook(il.pointer(m));
  if (status != MH_OK)
    throw Error("hook_install_failed", MH_StatusToString(status));
  std::lock_guard<std::mutex> l(mutex_);
  state_ = "bootstrap_pending";
}

// Unity 2019's flattened PlayerLoopSystemInternal stores a pointer to a native
// function slot, not the function address. The matched Unity 2019.4.40f1c1
// runtime was observed with root.count == array.length-1: counts are flattened
// descendants in this ABI, not immediate children. Validate every subtree span.
void Runtime::install_loop() {
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
  size_t input = n;
  for (size_t i = 0; i < n; i++)
    if (nodes[i].type) {
      auto name = il.utf8(il.call(nodes[i].type, "get_FullName"));
      if (name == "UnityEngine.PlayerLoop.EarlyUpdate+UpdateInputManager")
        input = i;
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
  const size_t insert = input + 1;
  for (size_t p = parents[input]; p < n; p = parents[p])
    nodes[p].count++;
  nodes.insert(nodes.begin() + insert, Node{nullptr, nullptr, &publish_slot, nullptr, 0, 0});
  nodes[0].count++;
  nodes.push_back(Node{nullptr, nullptr, &complete_slot, nullptr, 0, 0});
  Root replacement(il.array(node, nodes.size()));
  memcpy(il.data(replacement.get()), nodes.data(), nodes.size() * sizeof(Node));
  il.stat(loop, "SetPlayerLoopInternal", {replacement.get()});
  loop_root_ = std::move(replacement);
  loop_installed_ = true;
  install_input_hooks();
  std::lock_guard<std::mutex> l(mutex_);
  state_ = "observing";
}
void Runtime::uninstall_loop() {
  if (!loop_installed_)
    return;
  struct Node {
    Obj type, delegate;
    void *function;
    void *condition;
    int count;
    int pad;
  };
  auto loop = il.klass("UnityEngine.LowLevel", "PlayerLoop"),
       node = il.klass("UnityEngine.LowLevel", "PlayerLoopSystemInternal");
  Root a(il.stat(loop, "GetCurrentPlayerLoopInternal"));
  auto p = static_cast<Node *>(il.data(a.get()));
  std::vector<Node> nodes(p, p + il.length(a.get()));
  std::vector<size_t> parents(nodes.size(), nodes.size()), stack;
  if (nodes.empty() || nodes[0].count != int(nodes.size() - 1))
    throw Error("loop_layout_unsupported", "Invalid PlayerLoop root span");
  for (size_t i = 0; i < nodes.size(); i++) {
    while (!stack.empty() && stack.back() + size_t(nodes[stack.back()].count) < i)
      stack.pop_back();
    if (nodes[i].count < 0 || i + size_t(nodes[i].count) >= nodes.size())
      throw Error("loop_layout_unsupported", "Invalid PlayerLoop subtree span");
    if (!stack.empty())
      parents[i] = stack.back();
    if (nodes[i].count)
      stack.push_back(i);
  }
  for (size_t i = nodes.size(); i-- > 0;)
    if (nodes[i].function == &publish_slot || nodes[i].function == &complete_slot) {
      if (nodes[i].count || parents[i] >= parents.size())
        throw Error("loop_layout_unsupported", "Bridge node shape changed");
      for (size_t p = parents[i]; p < parents.size(); p = parents[p])
        nodes[p].count--;
      nodes.erase(nodes.begin() + i);
    }
  Root replacement(il.array(node, nodes.size()));
  memcpy(il.data(replacement.get()), nodes.data(), nodes.size() * sizeof(Node));
  il.stat(loop, "SetPlayerLoopInternal", {replacement.get()});
  loop_root_ = std::move(replacement);
  loop_installed_ = false;
}
void Runtime::event_update(Obj es, Method m) {
  bool saved = false, changed = false;
  try {
    if (!initialized_) {
      initialized_ = true;
      install_loop();
    }
    last_event_ = es;
    if (loop_installed_)
      event_observed_++;
    if (owned_) {
      saved = il.get<bool>(es, "m_HasFocus");
      il.set<bool>(es, "m_HasFocus", true);
      changed = true;
    }
  } catch (const std::exception &e) {
    fail("bootstrap_failed", e.what());
  }
  // Direct hand gestures have their own dispatch owner; other EventSystems still
  // run normally. The target EventSystem resumes on the frame after EndDrag.
  if (!direct_)
    original_event_update(es, m);
  if (changed)
    il.set<bool>(es, "m_HasFocus", saved);
}
void Runtime::observe() {
  if (++observed_ < 3)
    return;
  if (event_observed_ == 0)
    throw Error("loop_order_unobserved",
                "No EventSystem consumer observed between publish and completion");
  std::lock_guard<std::mutex> l(mutex_);
  if (state_ == "observing") {
    state_ = "ready";
    // Qualification is a release profile decision, not inferred from hook install.
    auto qualified = profile_.value("qualified_capabilities", Json::object());
    for (auto it = capabilities_.begin(); it != capabilities_.end(); ++it)
      it.value() = qualified.value(it.key(), false);
  }
}
Json Runtime::result() {
  return {{"state", state_},
          {"capabilities", capabilities_},
          {"viewport", {{"width", width_}, {"height", height_}, {"generation", generation_}}},
          {"position", {client_position().x, client_position().y}},
          {"frame", frame_},
          {"completed_frame", completed_frame_},
          {"error", error_}};
}
Vec2 Runtime::client_position() const {
  auto p = position();
  if (width_ <= 0 || height_ <= 0)
    return {};
  return {p.x, float(height_) - p.y};
}
Vec2 Runtime::coordinate(double x, double y) const {
  return {float(std::clamp(x, 0.0, double(width_ - 1))),
          float(height_) - float(std::clamp(y, 0.0, double(height_ - 1)))};
}
void Runtime::verify_viewport(const Json &v) {
  if (!IsWindow(window_) || IsIconic(window_))
    throw Error("viewport_invalid", "Game window unavailable or minimized");
  DWORD pid{};
  GetWindowThreadProcessId(window_, &pid);
  if (pid != GetCurrentProcessId())
    throw Error("viewport_invalid", "Window belongs to another process");
  RECT r{};
  if (!GetClientRect(window_, &r) || r.right <= 0 || r.bottom <= 0)
    throw Error("viewport_invalid", "Invalid client area");
  if (r.right != width_ || r.bottom != height_ || v.value("generation", generation_) != generation_)
    throw Error("viewport_changed", "Viewport changed during input");
  auto screen = il.klass("UnityEngine", "Screen");
  int w = il.scalar<int>(il.stat(screen, "get_width")),
      h = il.scalar<int>(il.stat(screen, "get_height"));
  if (w != width_ || h != height_)
    throw Error("viewport_mapping_unsupported",
                "Render and client dimensions must match this profile");
}
void Runtime::reply(const std::shared_ptr<Command> &c, const std::string &status,
                    const std::string &code, const std::string &message, Json r) {
  Json q = {{"version", 1},
            {"sequence", c->request.value("sequence", uint64_t(0))},
            {"session", c->request.value("session", std::string{})},
            {"status", status},
            {"result", std::move(r)}};
  if (!code.empty())
    q["error"] = {{"code", code}, {"message", message}};
  {
    std::lock_guard<std::mutex> l(c->mutex);
    if (c->done)
      return;
    c->response = std::move(q);
    c->done = true;
  }
  c->cv.notify_all();
}
void Runtime::prune_history() {
  while (history_.size() >= 4096) {
    auto victim = history_.end();
    // Sequence order is insertion age for the client protocol. An older command
    // can still be in flight when a cancellation overtakes it: never evict it.
    for (auto it = history_.begin(); it != history_.end(); ++it) {
      std::lock_guard<std::mutex> command_lock(it->second->mutex);
      if (it->second->done) {
        victim = it;
        break;
      }
    }
    if (victim == history_.end())
      break;
    retired_sequence_floor_ = std::max(retired_sequence_floor_, victim->first);
    history_.erase(victim);
  }
}
Json Runtime::submit(const Json &q) {
  auto c = std::make_shared<Command>();
  c->request = q;
  c->queued = now();
  auto timeout = std::clamp(q.value("timeout_ms", 30000), 1, 300000);
  c->deadline = c->queued + timeout;
  std::string op = q.value("op", std::string{}), session = q.value("session", std::string{});
  auto seq = q.value("sequence", uint64_t(0));
  {
    std::lock_guard<std::mutex> l(mutex_);
    if (q.value("version", 0) != 1 || session.empty() || !seq) {
      reply(c, "rejected", "protocol_invalid", "Version, session and sequence are required");
    } else if (op == "status") {
      reply(c, "completed", "", "",
            {{"state", state_},
             {"capabilities", capabilities_},
             {"last_tick", last_tick_.load()},
             {"error", error_}});
    } else if (state_ != "ready" && op != "cancel" && op != "close") {
      reply(c, "rejected", state_ == "failed" ? "bridge_failed" : "bootstrap_pending", error_);
    } else if (!session_.empty() && session_ != session) {
      reply(c, "rejected", "input_session_busy", "Another session owns the bridge");
    } else if (owner_pid_ && q.value("_client_pid", DWORD(0)) != owner_pid_) {
      reply(c, "rejected", "session_invalid", "Session belongs to another client process");
    } else {
      if (session_.empty()) {
        if (op != "hello")
          reply(c, "rejected", "session_invalid", "Hello required");
        else {
          session_ = session;
          history_.clear();
          cancel_fence_ = 0;
          retired_sequence_floor_ = 0;
        }
      }
      if (seq < cancel_fence_)
        reply(c, "rejected", "input_cancelled", "Command predates cancellation fence");
      if (seq <= retired_sequence_floor_)
        reply(c, "rejected", "input_sequence_expired", "Sequence is outside the retained replay window");
      if (!c->done) {
        auto found = history_.find(seq);
        if (found != history_.end()) {
          if (found->second->request != q)
            reply(c, "rejected", "sequence_conflict", "Sequence reused with another payload");
          else
            c = found->second;
        } else {
          const bool control = op == "cancel" || op == "close";
          if (control) {
            cancel_fence_ = std::max(cancel_fence_, seq);
            if (active_)
              active_->cancelled = true;
            for (auto &x : queue_) {
              x->cancelled = true;
              reply(x, "rejected", "input_cancelled", "Cancelled before execution");
            }
            queue_.clear();
          }
          prune_history();
          if (seq <= retired_sequence_floor_) {
            reply(c, "rejected", "input_sequence_expired", "Sequence is outside the retained replay window");
          } else if (history_.size() < 4096) {
            history_[seq] = c;
          } else if (control) {
            // Defensive saturation path: recovery must never depend on having
            // replay-cache space. Keep the live command in queue_/active_ only;
            // retire its sequence immediately so a retry cannot execute it twice.
            retired_sequence_floor_ = std::max(retired_sequence_floor_, seq);
          } else {
            reply(c, "rejected", "input_queue_full", "All replay records are still in flight");
          }
          if (!c->done) {
            if (control)
              queue_.push_front(c);
            else if (queue_.size() >= 64)
              reply(c, "rejected", "input_queue_full", "Input queue is full");
            else
              queue_.push_back(c);
          }
        }
      }
    }
  }
  std::unique_lock<std::mutex> l(c->mutex);
  if (!c->cv.wait_for(l, std::chrono::milliseconds(timeout), [&] { return c->done; })) {
    c->cancelled = true;
    return {{"version", 1},
            {"session", session},
            {"sequence", seq},
            {"status", "unknown"},
            {"error",
             {{"code", "frame_stalled"},
              {"message", "Completion not observed; operation will not be replayed"}}}};
  }
  return c->response;
}
void Runtime::publish() {
  last_tick_ = now();
  frame_++;
  event_observed_ = 0;
  try {
    if (owner_process_ && WaitForSingleObject(owner_process_, 0) == WAIT_OBJECT_0) {
      cleanup(true);
      finish("rejected", "input_owner_exited", "Input owner exited");
      restore();
      std::lock_guard<std::mutex> l(mutex_);
      for (auto &q : queue_)
        reply(q, "rejected", "input_owner_exited", "Input owner exited");
      queue_.clear();
      session_.clear();
      history_.clear();
      retired_sequence_floor_ = 0;
      cancel_fence_ = 0;
      return;
    }
    {
      std::lock_guard<std::mutex> l(snapshot_mutex_);
      snapshot_.down = snapshot_.up = 0;
      snapshot_.wheel = {};
    }
    if (observed_ < 3)
      return;
    if (active_ && (active_->cancelled || now() > active_->deadline)) {
      cleanup(true);
      finish("rejected", "input_cancelled", "Operation cancelled");
    }
    if (!active_) {
      std::shared_ptr<Command> next;
      {
        std::lock_guard<std::mutex> l(mutex_);
        if (!queue_.empty()) {
          next = queue_.front();
          queue_.pop_front();
          active_ = next;
        }
      }
      if (next)
        begin(next);
    }
    if (active_)
      tick();
  } catch (const Error &e) {
    cleanup(true);
    finish("rejected", e.code, e.what());
  } catch (const std::exception &e) {
    cleanup(true);
    finish("rejected", "bridge_runtime_error", e.what());
  }
}
void Runtime::complete() {
  completed_frame_ = frame_;
  try {
    if (observed_ < 3) {
      observe();
      return;
    }
    if (active_ && stage_ == 99) {
      finish();
    }
  } catch (const std::exception &e) {
    fail("loop_observation_failed", e.what());
  }
}
void Runtime::begin(const std::shared_ptr<Command> &c) {
  op_ = c->request.at("op");
  const auto &a = c->request.value("args", Json::object());
  stage_ = 0;
  click_index_ = 0;
  stage_frame_ = frame_;
  started_ = stage_time_ = now();
  if (op_ == "hello") {
    auto raw = a.value("hwnd", Json(0));
    window_ = reinterpret_cast<HWND>(raw.is_string() ? std::stoull(raw.get<std::string>())
                                                     : raw.get<uint64_t>());
    auto v = c->request.value("viewport", a.value("viewport", Json::object()));
    width_ = v.at("width");
    height_ = v.at("height");
    generation_ = v.value("generation", uint64_t(1));
    verify_viewport(v);
    if (!owner_process_) {
      DWORD owner = c->request.value("_client_pid", DWORD(0));
      if (!owner)
        throw Error("session_invalid", "Pipe client identity unavailable");
      owner_process_ = OpenProcess(SYNCHRONIZE, FALSE, owner);
      if (!owner_process_)
        throw Error("session_invalid", "Cannot monitor input owner lifetime");
      owner_pid_ = owner;
    }
    if (!background_saved_) {
      background_value_ =
          il.scalar<bool>(il.stat(il.klass("UnityEngine", "Application"), "get_runInBackground"));
      background_saved_ = true;
    }
    bool yes = true;
    il.stat(il.klass("UnityEngine", "Application"), "set_runInBackground", {&yes});
    if (owned_ && a.contains("position")) {
      auto p = a.at("position");
      set_position(coordinate(p.at(0).get<double>(), p.at(1).get<double>()));
    } else if (!owned_)
      set_position(coordinate(width_ / 2, height_ / 2));
    owned_ = true;
    stage_ = 99;
    return;
  }
  if (op_ == "close") {
    cleanup(true);
    restore();
    stage_ = 99;
    return;
  }
  if (op_ == "cancel") {
    cleanup(true);
    stage_ = 99;
    return;
  }
  if (!owned_)
    throw Error("session_invalid", "Input ownership not acquired");
  verify_viewport(c->request.value("viewport", Json::object()));
  if (op_ == "release_all") {
    std::lock_guard<std::mutex> l(snapshot_mutex_);
    snapshot_.up = snapshot_.held;
    snapshot_.held = 0;
    stage_ = 99;
    return;
  }
  const bool full = op_ == "click" || op_ == "drag";
  if (full) {
    std::lock_guard<std::mutex> l(snapshot_mutex_);
    if (snapshot_.held)
      throw Error("input_gesture_busy", "Buttons are already held");
  }
  const auto cap = op_ == "drag" ? "direct_drag" : op_ == "scroll" ? "scroll" : "absolute_pointer";
  if (!capabilities_.value(cap, false))
    throw Error("input_capability_unsupported", std::string("Capability is not qualified: ") + cap);
  button_ = a.value("button", 0);
  if (button_ < 0 || button_ > 2)
    throw Error("unsupported_mouse_button", "Button must be 0, 1 or 2");
  duration_ = std::max(number(a, "duration"), 0.0);
  hold_ = std::max(number(a, "hold"), 0.0);
  auto p = client_position();
  start_ =
      coordinate(a.contains("x") ? number(a, "x") : p.x, a.contains("y") ? number(a, "y") : p.y);
  end_ = start_;
  if (op_ == "move_relative")
    end_ = coordinate(p.x + number(a, "dx"), p.y + number(a, "dy"));
  else if (op_ == "drag")
    end_ = coordinate(number(a, "to_x"), number(a, "to_y"));
  else if (op_ == "move")
    end_ = start_;
  if (op_ == "move" || op_ == "move_relative") {
    auto current = position();
    start_ = {current.x, current.y};
    return;
  }
  if (op_ == "click") {
    set_position(start_);
    return;
  }
  if (op_ == "button_down" || op_ == "button_up") {
    std::lock_guard<std::mutex> l(snapshot_mutex_);
    auto bit = uint8_t(1 << button_);
    if (op_ == "button_down") {
      snapshot_.held |= bit;
      snapshot_.down |= bit;
    } else {
      snapshot_.held &= ~bit;
      snapshot_.up |= bit;
    }
    stage_ = 99;
    return;
  }
  if (op_ == "scroll") {
    std::lock_guard<std::mutex> l(snapshot_mutex_);
    snapshot_.wheel = {0, float(number(a, "signed_detents"))};
    stage_ = 99;
    return;
  }
  if (op_ == "drag") {
    set_position(start_);
    direct_ = true;
    {
      std::lock_guard<std::mutex> l(snapshot_mutex_);
      snapshot_.held = uint8_t(1 << button_);
    }
    resolve_drag();
    dispatch("OnInitializePotentialDrag", true);
    dispatch("OnBeginDrag");
    drag_begun_ = true;
    return;
  }
  throw Error("input_capability_unsupported", "Unknown or unsupported input operation");
}
void Runtime::tick() {
  if (stage_ == 99)
    return;
  verify_viewport(active_->request.value("viewport", Json::object()));
  const auto &a = active_->request.value("args", Json::object());
  double elapsed = double(now() - started_) / 1000.0;
  if (op_ == "click") {
    if (stage_ == 0 && frame_ > stage_frame_) {
      std::lock_guard<std::mutex> l(snapshot_mutex_);
      snapshot_.held |= 1 << button_;
      snapshot_.down |= 1 << button_;
      stage_ = 1;
      stage_frame_ = frame_;
    } else if (stage_ == 1 && frame_ > stage_frame_) {
      std::lock_guard<std::mutex> l(snapshot_mutex_);
      snapshot_.held &= ~(1 << button_);
      snapshot_.up |= 1 << button_;
      stage_ = 2;
      stage_frame_ = frame_;
      stage_time_ = now();
    } else if (stage_ == 2 && frame_ > stage_frame_ &&
               now() - stage_time_ >= std::max(number(a, "post_delay"), 0.0) * 1000) {
      if (++click_index_ >= std::max(a.value("clicks", 1), 1))
        stage_ = 99;
      else {
        stage_ = 3;
        stage_time_ = now();
      }
    } else if (stage_ == 3 && now() - stage_time_ >= std::max(number(a, "interval"), 0.0) * 1000) {
      stage_ = 0;
      stage_frame_ = frame_ - 1;
    }
    return;
  }
  if (op_ == "move" || op_ == "move_relative" || op_ == "drag") {
    if (op_ == "drag" && frame_ <= stage_frame_)
      return;
    double ratio = duration_ <= 0 ? 1 : std::min(elapsed / duration_, 1.0);
    if (duration_ > 0) {
      int n = std::max(int(duration_ / .02), 1);
      ratio = std::min(1.0, (std::floor(ratio * n) + 1) / n);
    }
    Vec2 p{float(ties_even(start_.x + (end_.x - start_.x) * ratio)),
           float(height_ - ties_even(height_ - (start_.y + (end_.y - start_.y) * ratio)))};
    auto previous = position();
    set_position(p);
    if (op_ == "drag") {
      if (!current_target_valid())
        throw Error("drag_target_destroyed", "Drag target or EventSystem changed");
      if (frame_ > stage_frame_) {
        set_event_position(p, {p.x - previous.x, p.y - previous.y});
        dispatch("OnDrag");
      }
      if (elapsed >= duration_ + hold_ && frame_ > stage_frame_) {
        {
          std::lock_guard<std::mutex> l(snapshot_mutex_);
          snapshot_.held = 0;
        }
        dispatch("OnEndDrag");
        drag_begun_ = false;
        if (scroll_target_ && a.value("stop_inertia", true))
          dispatch("StopMovement");
        cleanup(false);
        stage_ = 99;
      }
    } else if (elapsed >= duration_ && frame_ > stage_frame_)
      stage_ = 99;
  }
}
void Runtime::finish(const std::string &status, const std::string &code,
                     const std::string &message) {
  if (!active_)
    return;
  auto c = active_;
  bool closing = op_ == "close";
  Json r = result();
  reply(c, status, code, message, std::move(r));
  {
    std::lock_guard<std::mutex> l(mutex_);
    active_.reset();
    if (closing) {
      session_.clear();
      history_.clear();
      retired_sequence_floor_ = 0;
      cancel_fence_ = 0;
    }
  }
  op_.clear();
}
void Runtime::set_event_position(Vec2 p, Vec2 d) {
  set_prop(event_.get(), "set_position", &p);
  set_prop(event_.get(), "set_delta", &d);
}
void Runtime::dispatch(const char *name, bool optional) {
  if (!target_.get())
    return;
  if (std::strcmp(name, "StopMovement") == 0) {
    il.call(target_.get(), name);
    return;
  }
  if (scroll_target_) {
    try {
      il.call(target_.get(), name, {event_.get()});
    } catch (const Error &e) {
      if (optional && e.code == "version_mismatch")
        return;
      throw;
    }
    return;
  }
  // Unity dispatches to all active components implementing the event interface
  // on the selected GameObject; the handlers need not live on the drag component.
  const char *iface = nullptr;
  if (std::strcmp(name, "OnInitializePotentialDrag") == 0)
    iface = "IInitializePotentialDragHandler";
  else if (std::strcmp(name, "OnBeginDrag") == 0)
    iface = "IBeginDragHandler";
  else if (std::strcmp(name, "OnDrag") == 0)
    iface = "IDragHandler";
  else if (std::strcmp(name, "OnEndDrag") == 0)
    iface = "IEndDragHandler";
  else if (std::strcmp(name, "OnPointerDown") == 0)
    iface = "IPointerDownHandler";
  else if (std::strcmp(name, "OnPointerUp") == 0)
    iface = "IPointerUpHandler";
  if (!iface)
    throw Error("input_capability_unsupported", "Unknown event handler contract");
  Obj go = game_object(target_.get());
  auto contract = il.klass("UnityEngine.EventSystems", iface);
  Root handlers(il.invoke(il.typed_method(il.object_class(go), "GetComponents", {"System.Type"}),
                          go, {il.type(contract)}));
  auto p = static_cast<Obj *>(il.data(handlers.get()));
  bool sent = false;
  for (size_t i = 0; i < il.length(handlers.get()); i++)
    if (active(p[i])) {
      il.call_interface(p[i], contract, name, {event_.get()});
      sent = true;
    }
  if (!sent && !optional && std::strcmp(name, "OnDrag") == 0)
    throw Error("drag_target_missing", "Drag handler disappeared");
}
bool Runtime::current_target_valid() {
  if (active_scene_handle() != drag_scene_)
    throw Error("input_scene_changed", "Active scene changed during drag");
  return active(target_.get()) && active(raycaster_.get()) && active(gesture_event_system_.get()) &&
         il.stat(il.klass("UnityEngine.EventSystems", "EventSystem"), "get_current") ==
             gesture_event_system_.get();
}
void Runtime::resolve_drag() {
  auto es = il.stat(il.klass("UnityEngine.EventSystems", "EventSystem"), "get_current");
  if (!es)
    throw Error("drag_target_missing", "No current EventSystem");
  gesture_event_system_.reset(es);
  drag_scene_ = active_scene_handle();
  event_.reset(il.create(il.klass("UnityEngine.EventSystems", "PointerEventData")));
  il.call(event_.get(), ".ctor", {es});
  int id = -1 - button_;
  bool no = false;
  set_prop(event_.get(), "set_pointerId", &id);
  set_prop(event_.get(), "set_button", &button_);
  set_prop(event_.get(), "set_eligibleForClick", &no);
  set_prop(event_.get(), "set_useDragThreshold", &no);
  set_prop(event_.get(), "set_pressPosition", &start_);
  set_event_position(start_, {});
  auto ray = il.method(il.object_class(es), "RaycastAll", 2);
  Root hits(il.create(il.param_class(ray, 1)));
  il.call(hits.get(), ".ctor");
  il.invoke(ray, es, {event_.get(), hits.get()});
  int count = il.scalar<int>(il.call(hits.get(), "get_Count"));
  if (count <= 0)
    throw Error("drag_target_missing", "No raycast hit");
  int first = 0;
  Root hit(il.call(hits.get(), "get_Item", {&first}));
  Obj go = il.get<Obj>(hit.get(), "m_GameObject"), caster = il.get<Obj>(hit.get(), "module");
  if (!il.alive(go) || !il.alive(caster))
    throw Error("drag_target_missing", "Invalid top raycast hit");
  raycaster_.reset(caster);
  il.call(event_.get(), "set_pointerPressRaycast", {il.unbox(hit.get())});
  il.call(event_.get(), "set_pointerCurrentRaycast", {il.unbox(hit.get())});
  auto drag_interface = il.klass("UnityEngine.EventSystems", "IDragHandler"),
       scroll = il.klass("UnityEngine.UI", "ScrollRect");
  Obj candidate{};
  for (Obj t = transform(go); t; t = parent(t)) {
    auto c = component(game_object(t), drag_interface);
    if (c) {
      if (!active(c))
        throw Error("drag_target_disabled", "Top drag handler disabled");
      candidate = c;
      break;
    }
  }
  if (!candidate) {
    Root all(il.stat(il.klass("UnityEngine", "Object"), "FindObjectsOfType", {il.type(scroll)}));
    auto items = static_cast<Obj *>(il.data(all.get()));
    Obj ui_branch = branch(go);
    Obj camera = il.call(caster, "get_eventCamera");
    for (size_t i = 0; i < il.length(all.get()); i++) {
      Obj c = items[i];
      if (!active(c) || !ui_branch || branch(game_object(c)) != ui_branch)
        continue;
      Obj viewport = il.call(c, "get_viewport");
      if (!viewport)
        viewport = transform(c);
      if (!contains(viewport, start_, camera))
        continue;
      if (candidate)
        throw Error("drag_target_ambiguous", "Multiple visible ScrollRects contain drag origin");
      candidate = c;
    }
  }
  if (!candidate)
    throw Error("drag_target_missing", "Top hit has no compatible drag handler");
  target_.reset(candidate);
  scroll_target_ = il.derives(candidate, scroll);
  Obj target_go = game_object(candidate);
  set_prop(event_.get(), "set_pointerDrag", target_go);
  bool yes = true;
  set_prop(event_.get(), "set_dragging", &yes);
  // Generic handlers can depend on their own PointerDown/Up contract. ScrollRect
  // intentionally receives no item down/up and therefore cannot synthesize click.
  if (!scroll_target_)
    dispatch("OnPointerDown", true);
}
void Runtime::clear_module_pointer() {
  Obj es = il.stat(il.klass("UnityEngine.EventSystems", "EventSystem"), "get_current");
  if (!es)
    return;
  Obj module = il.call(es, "get_currentInputModule");
  if (!module)
    return;
  for (int id = -1; id >= -3; --id) {
    Obj p{};
    bool create = false;
    try {
      il.call(module, "GetPointerData", {&id, &p, &create});
    } catch (const Error &e) {
      if (e.code == "version_mismatch")
        return;
      throw;
    }
    if (!p)
      continue;
    bool no = false;
    set_prop(p, "set_eligibleForClick", &no);
    set_prop(p, "set_pointerPress", nullptr);
    set_prop(p, "set_rawPointerPress", nullptr);
    Obj drag = il.call(p, "get_pointerDrag");
    if (drag) {
      Obj handler = component(drag, il.klass("UnityEngine.EventSystems", "IEndDragHandler"));
      if (handler)
        il.call(handler, "OnEndDrag", {p});
    }
    set_prop(p, "set_pointerDrag", nullptr);
    set_prop(p, "set_dragging", &no);
  }
}
void Runtime::cleanup(bool cancel) {
  try {
    if (drag_begun_ && il.alive(target_.get()))
      dispatch("OnEndDrag");
    if (!cancel && !scroll_target_ && target_.get())
      dispatch("OnPointerUp", true);
    if (cancel) {
      if (scroll_target_ && il.alive(target_.get()))
        dispatch("StopMovement");
      clear_module_pointer();
    }
  } catch (const std::exception &e) {
    std::lock_guard<std::mutex> l(mutex_);
    error_ = std::string("cleanup_incomplete: ") + e.what();
  }
  // Managed cleanup can fail after a scene change. Native held state must still
  // be cleared so the next consumer never receives an orphaned virtual press.
  if (cancel || direct_) {
    std::lock_guard<std::mutex> l(snapshot_mutex_);
    snapshot_.held = snapshot_.down = snapshot_.up = 0;
  }
  drag_begun_ = false;
  event_.reset();
  target_.reset();
  raycaster_.reset();
  gesture_event_system_.reset();
  scroll_target_ = false;
  direct_ = false;
}
void Runtime::restore() {
  owned_ = false;
  if (owner_process_) {
    CloseHandle(owner_process_);
    owner_process_ = nullptr;
    owner_pid_ = 0;
  }
  if (background_saved_) {
    il.stat(il.klass("UnityEngine", "Application"), "set_runInBackground", {&background_value_});
    background_saved_ = false;
  }
}
void Runtime::fail(std::string code, std::string reason) {
  owned_ = false;
  direct_ = false;
  std::lock_guard<std::mutex> l(mutex_);
  state_ = "failed";
  error_ = code + ": " + reason;
}
} // namespace bridge
