#pragma once
#include "il2cpp.hpp"
#include <atomic>
#include <condition_variable>
#include <deque>
#include <memory>
#include <map>
#include <mutex>
#include <nlohmann/json.hpp>

namespace bridge {
using Json = nlohmann::json;
struct Command {
  Json request, response;
  std::mutex mutex;
  std::condition_variable cv;
  bool done{};
  std::atomic<bool> cancelled{false};
  uint64_t queued{}, deadline{};
};
class Runtime {
public:
  void initialize(const Json &profile);
  Json submit(const Json &q);
  void publish();
  void complete();
  void event_update(Obj es, Method method);
  void fail(std::string code, std::string reason);
  bool owns() const { return owned_.load(); }
  bool exclusive_drag() const { return direct_.load(); }
  Vec3 position() const;
  Vec2 wheel() const;
  bool button(int key, int phase) const;

private:
  struct Snapshot {
    Vec3 position{};
    Vec2 wheel{};
    uint8_t held{}, down{}, up{};
  } snapshot_;
  mutable std::mutex snapshot_mutex_;
  std::mutex mutex_;
  std::deque<std::shared_ptr<Command>> queue_;
  std::map<uint64_t, std::shared_ptr<Command>> history_;
  uint64_t retired_sequence_floor_{};
  std::string session_, state_ = "loaded", error_;
  HWND window_{};
  uint64_t cancel_fence_{};
  HANDLE owner_process_{};
  std::atomic<DWORD> owner_pid_{0};
  int width_{}, height_{};
  uint64_t generation_{}, last_frame_{}, frame_{}, completed_frame_{};
  std::atomic<bool> owned_{false}, direct_{false};
  std::atomic<uint64_t> last_tick_{0};
  bool initialized_{}, loop_installed_{}, background_saved_{}, background_value_{};
  int observed_{}, event_observed_{};
  Obj last_event_{};
  Root loop_root_;
  Json profile_, capabilities_;
  std::shared_ptr<Command> active_;
  std::string op_;
  int stage_{}, click_index_{};
  uint64_t started_{}, stage_time_{}, stage_frame_{};
  Vec2 start_{}, end_{};
  int button_{};
  double duration_{}, hold_{};
  Root event_, target_, raycaster_;
  bool scroll_target_{}, drag_begun_{};
  int drag_scene_{};
  Root gesture_event_system_;
  void install_loop();
  void uninstall_loop();
  void observe();
  void begin(const std::shared_ptr<Command> &);
  void tick();
  void finish(const std::string &status = "completed", const std::string &code = "",
              const std::string &message = "");
  void cleanup(bool cancel);
  void clear_module_pointer();
  void restore();
  void prune_history(); // Caller holds mutex_; only completed records are evicted.
  void reply(const std::shared_ptr<Command> &, const std::string &, const std::string &code = "",
             const std::string &message = "", Json result = Json::object());
  Json result();
  void verify_viewport(const Json &);
  Vec2 client_position() const;
  Vec2 coordinate(double x, double y) const;
  void set_position(Vec2 p);
  void resolve_drag();
  void dispatch(const char *name, bool optional = false);
  void set_event_position(Vec2 p, Vec2 delta);
  bool current_target_valid();
};
extern Runtime &runtime;
void install_input_hooks();
void disable_input_hooks();
using EventUpdateFn = void (*)(Obj, Method);
extern EventUpdateFn original_event_update;
} // namespace bridge
