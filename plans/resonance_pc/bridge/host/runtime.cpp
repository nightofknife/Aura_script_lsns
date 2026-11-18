#include "runtime.hpp"
#include "deep_dive_board.hpp"
#include <algorithm>
#include <cmath>
#include <cstring>

namespace bridge {
namespace {
uint64_t now() { return GetTickCount64(); }
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
void Runtime::initialize(Json profile, bool capture) {
  il.load();
  profile_ = std::move(profile);
  capture_loop_installed_ = capture;
  capabilities_ = {{"absolute_pointer", false}, {"direct_drag", false}, {"scroll", false},
                   {"relative_look", false}, {"keyboard", false}, {"text_input", false},
                   {"background_input", false}, {"frame_stream", false}};
  state_ = "observing";
}

bool Runtime::idle() {
  std::lock_guard<std::mutex> lock(mutex_);
  return session_.empty() && !active_ && queue_.empty() && !owned_ &&
         !background_saved_ && !frame_stream_.enabled();
}

void Runtime::event_update(Obj es, Method method, EventUpdateFn original) {
  if (direct_)
    return;
  event_observed_++;
  if (!owned_) {
    original(es, method);
    return;
  }
  bool saved{};
  try {
    saved = il.get<bool>(es, "m_HasFocus");
    il.set<bool>(es, "m_HasFocus", true);
  } catch (const std::exception &e) {
    fail("focus_override_failed", e.what());
    original(es, method);
    return;
  }
  original(es, method);
  try {
    il.set<bool>(es, "m_HasFocus", saved);
  } catch (const std::exception &e) {
    fail("focus_restore_failed", e.what());
  }
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
    capabilities_["frame_stream"] = capture_loop_installed_ && qualified.value("frame_stream", false);
    capabilities_["deep_dive_board"] = deep_dive_board_available();
    capabilities_["deep_dive_turn"] = capabilities_["deep_dive_board"];
    capabilities_["deep_dive_controls"] = capabilities_["deep_dive_board"];
  }
}
Json Runtime::result() {
  Json viewport = {{"width", width_}, {"height", height_}, {"generation", generation_}};
  if (IsIconic(window_))
    viewport["minimized"] = true;
  return {{"state", state_},
          {"capabilities", capabilities_},
          {"viewport", std::move(viewport)},
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
void Runtime::verify_viewport(const Json &v, bool handshake) {
  if (!IsWindow(window_))
    throw Error("viewport_invalid", "Game window unavailable");
  DWORD pid{};
  GetWindowThreadProcessId(window_, &pid);
  if (pid != GetCurrentProcessId())
    throw Error("viewport_invalid", "Window belongs to another process");
  if (v.value("generation", generation_) != generation_)
    throw Error("viewport_changed", "Viewport changed during input");
  auto screen = il.klass("UnityEngine", "Screen");
  int w = il.scalar<int>(il.stat(screen, "get_width")),
      h = il.scalar<int>(il.stat(screen, "get_height"));
  if (w <= 0 || h <= 0)
    throw Error("viewport_invalid", "Unity has no usable input dimensions");
  if (handshake && width_ == 0 && height_ == 0) {
    width_ = w;
    height_ = h;
  }
  if (w != width_ || h != height_)
    throw Error("viewport_changed", "Unity render dimensions changed during input");
}

void Runtime::capture_frame() { frame_stream_.publish(frame_, window_); }
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
      if (next) {
        if (next->cancelled || now() > next->deadline) {
          op_ = next->request.at("op");
          finish("rejected", "input_cancelled", "Command expired before execution");
        } else {
          begin(next);
        }
      }
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
    verify_viewport(v, true);
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
  if (op_ == "stream_stop") {
    frame_stream_.close();
    stage_ = 99;
    return;
  }
  if (!owned_)
    throw Error("session_invalid", "Input ownership not acquired");
  if (op_ == "deep_dive_board") {
    // A read failure must leave the current pointer and held buttons alone.
    try {
      if (!capabilities_.value("deep_dive_board", false))
        throw Error("deep_dive_board_unsupported", "The client lacks the Deep Dive reader ABI");
      const bool controls = a.value("controls_only", false);
      if (controls)
        verify_viewport(c->request.value("viewport", Json::object()));
      finish("completed", "", "", read_deep_dive_board(a.value("turn_only", false), controls));
    } catch (const Error &e) {
      finish("rejected", e.code, e.what());
    } catch (const std::exception &e) {
      finish("rejected", "deep_dive_board_read_failed", e.what());
    }
    return;
  }
  verify_viewport(c->request.value("viewport", Json::object()));
  if (op_ == "stream_start") {
    if (!capabilities_.value("frame_stream", false))
      throw Error("bridge_capture_unsupported", "This Unity profile has no frame-end capture");
    frame_stream_.open(c->request.at("session"), owner_pid_, width_, height_);
    stage_ = 99;
    return;
  }
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
                     const std::string &message, Json board) {
  if (!active_)
    return;
  auto c = active_;
  bool failed_hello = op_ == "hello" && status != "completed" && !owned_;
  if (failed_hello) {
    try {
      restore();
    } catch (const std::exception &e) {
      std::lock_guard<std::mutex> lock(mutex_);
      error_ = std::string("cleanup_incomplete: ") + e.what();
    }
  }
  const bool closing = (op_ == "close" && status == "completed") ||
                       (failed_hello && !background_saved_ && !owner_process_);
  Json r = result();
  if (!board.is_null())
    r["board"] = std::move(board);
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
  reply(c, status, code, message, std::move(r));
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
  bool disabled_handler = false;
  for (Obj t = transform(go); t; t = parent(t)) {
    auto c = component(game_object(t), drag_interface);
    if (!c)
      continue;
    if (!active(c)) {
      disabled_handler = true;
      continue;
    }
    candidate = c;
    break;
  }
  if (!candidate) {
    if (disabled_handler)
      throw Error("drag_target_disabled", "Top drag handler disabled");
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
  frame_stream_.close();
  owned_ = false;
  if (background_saved_) {
    il.stat(il.klass("UnityEngine", "Application"), "set_runInBackground", {&background_value_});
    background_saved_ = false;
  }
  if (owner_process_) {
    CloseHandle(owner_process_);
    owner_process_ = nullptr;
    owner_pid_ = 0;
  }
}
void Runtime::fail(std::string code, std::string reason) {
  frame_stream_.close();
  owned_ = false;
  direct_ = false;
  std::lock_guard<std::mutex> l(mutex_);
  state_ = "failed";
  error_ = code + ": " + reason;
}
} // namespace bridge
