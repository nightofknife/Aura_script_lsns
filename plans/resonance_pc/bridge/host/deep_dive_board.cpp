#include "deep_dive_board.hpp"
#include "deep_dive_event_choices.hpp"
#include "il2cpp.hpp"

#include <algorithm>
#include <limits>
#include <optional>
#include <string_view>

namespace bridge {
namespace {
using Json = nlohmann::json;
using LuaState = void;
constexpr int registry = -1001000;
constexpr int lua_nil = 0, lua_boolean = 1, lua_string = 4, lua_table = 5, lua_function = 6, lua_userdata = 7;
constexpr int face_count = 6;

struct LuaApi {
  const double *(*version)(LuaState *){};
  int (*top)(LuaState *){};
  void (*set_top)(LuaState *, int){};
  int (*check_stack)(LuaState *, int){};
  int (*type)(LuaState *, int){};
  const char *(*push_string)(LuaState *, const char *){};
  void (*push_integer)(LuaState *, int64_t){};
  void (*push_nil)(LuaState *){};
  void (*push_value)(LuaState *, int){};
  int (*raw_get)(LuaState *, int){};
  void (*raw_set)(LuaState *, int){};
  void (*create_table)(LuaState *, int, int){};
  int (*raw_get_index)(LuaState *, int, int64_t){};
  size_t (*raw_length)(LuaState *, int){};
  int (*next)(LuaState *, int){};
  int64_t (*integer)(LuaState *, int, int *){};
  int (*boolean)(LuaState *, int){};
  const char *(*string)(LuaState *, int, size_t *){};
  int (*load_string)(LuaState *, const char *){};
  int (*protected_call)(LuaState *, int, int, int, intptr_t, int (*)(LuaState *, int, intptr_t)){};

  LuaApi() {
    const auto module = GetModuleHandleW(L"xlua.dll");
    if (!module)
      throw Error("deep_dive_board_unsupported", "XLua is not loaded");
#define LUA(member, name)                                                                       \
    member = reinterpret_cast<decltype(member)>(GetProcAddress(module, "lua_" name));             \
    if (!member)                                                                                \
      throw Error("deep_dive_board_unsupported", "Missing Lua API " name)
    LUA(version, "version");
    LUA(top, "gettop");
    LUA(set_top, "settop");
    LUA(check_stack, "checkstack");
    LUA(type, "type");
    LUA(push_string, "pushstring");
    LUA(push_integer, "pushinteger");
    LUA(push_nil, "pushnil");
    LUA(push_value, "pushvalue");
    LUA(raw_get, "rawget");
    LUA(raw_set, "rawset");
    LUA(create_table, "createtable");
    LUA(raw_get_index, "rawgeti");
    LUA(raw_length, "rawlen");
    LUA(next, "next");
    LUA(integer, "tointegerx");
    LUA(boolean, "toboolean");
    LUA(string, "tolstring");
    LUA(protected_call, "pcallk");
#undef LUA
    load_string = reinterpret_cast<decltype(load_string)>(GetProcAddress(module, "luaL_loadstring"));
    if (!load_string)
      throw Error("deep_dive_board_unsupported", "Missing Lua API luaL_loadstring");
    const auto abi = version(nullptr);
    if (!abi || *abi != 503.0)
      throw Error("deep_dive_board_unsupported", "Expected the Lua 5.3 ABI");
  }
};

const LuaApi &lua_api() {
  static const LuaApi api;
  return api;
}

// All indexes used below are absolute, except the Lua registry pseudo-index.
// Board fields use raw reads and action limits use the client's own getters.
struct LuaView {
  const LuaApi &api;
  LuaState *state;
  int original_top;

  LuaView(const LuaApi &api, LuaState *state)
      : api(api), state(state), original_top(api.top(state)) {
    if (!api.check_stack(state, 24))
      throw Error("deep_dive_board_read_failed", "Lua stack capacity unavailable");
  }
  ~LuaView() { api.set_top(state, original_top); }

  int top() const { return api.top(state); }
  int type(int index) const { return api.type(state, index); }
  void restore(int index) const { api.set_top(state, index); }
  int field(int parent, const char *key) const {
    api.push_string(state, key);
    api.raw_get(state, parent);
    return top();
  }
  int at(int parent, int index) const {
    api.raw_get_index(state, parent, index);
    return top();
  }
  bool array(int index, int length) const {
    return type(index) == lua_table && api.raw_length(state, index) == size_t(length);
  }
  std::optional<int> number(int index) const {
    int numeric{};
    const auto value = api.integer(state, index, &numeric);
    if (!numeric || value < 0 || value > std::numeric_limits<int>::max())
      return std::nullopt;
    return int(value);
  }
  std::optional<int> number(int parent, const char *key) const {
    const auto index = field(parent, key);
    const auto value = number(index);
    restore(index - 1);
    return value;
  }
  std::optional<int> quota(int model, const char *getter, int argument = 0) const {
    const auto stack = top();
    const auto function = field(model, getter);
    std::optional<int> result;
    if (type(function) == lua_function) {
      if (argument)
        api.push_value(state, argument);
      if (api.protected_call(state, argument ? 1 : 0, 1, 0, 0, nullptr) == 0)
        result = number(top());
    }
    restore(stack);
    return result;
  }
  bool flag(int parent, const char *key, bool fallback = false) const {
    const auto index = field(parent, key);
    const auto value = type(index) == lua_boolean ? api.boolean(state, index) != 0 : fallback;
    restore(index - 1);
    return value;
  }
  std::string text(int parent, const char *key) const {
    const auto index = field(parent, key);
    size_t length{};
    const auto raw = type(index) == lua_string ? api.string(state, index, &length) : nullptr;
    const auto result = raw && length <= 120 ? std::string(raw, length) : std::string{};
    restore(index - 1);
    return result;
  }
};

[[noreturn]] void invalid_board(const char *reason) {
  throw Error("deep_dive_board_inconsistent", reason);
}

LuaState *current_lua_state() {
  const auto bus = il.static_object(il.klass("", "CBus"), "_instance");
  if (!bus || il.get<bool>(bus, "IsClearing") || il.get<bool>(bus, "IsDestroying"))
    return nullptr;
  const auto env = il.get<Obj>(bus, "luaEnv");
  if (!env || !il.derives(env, il.klass("XLua", "LuaEnv")) || il.get<bool>(env, "disposed"))
    return nullptr;
  return il.get<LuaState *>(env, "rawL");
}

const char *node_kind(int type_id) {
  switch (type_id) {
  case 12601540: return "battle";
  case 12601541: return "boss_battle";
  case 12601542: return "elite_battle";
  case 12601614: return "shop";
  case 12601615: return "healing";
  case 12601616: return "adventure";
  case 12601617: return "reward";
  case 12601699: return "special_shop";
  default: return "unknown";
  }
}

int cached_record(const LuaView &lua, int cache, int factory, int id) {
  if (lua.type(cache) != lua_table)
    return 0;
  const auto record = lua.at(cache, id);
  if (lua.type(record) == lua_nil && factory && lua.type(factory) == lua_table) {
    lua.restore(record - 1);
    return lua.at(factory, id);
  }
  return record;
}

void read_actor(const LuaView &lua, int model, const char *field, const char *kind,
                int size, Json &objects, bool required = false) {
  const auto index = lua.field(model, field);
  if (lua.type(index) == lua_table) {
    const auto face = lua.number(index, "face").value_or(0);
    const auto row = lua.number(index, "x").value_or(0);
    const auto col = lua.number(index, "y").value_or(0);
    if (face < 1 || face > face_count || row < 1 || row > size || col < 1 || col > size)
      invalid_board("Actor location is outside the board");
    objects.push_back({{"type", kind}, {"face", face}, {"row", row}, {"col", col}});
  } else if (required || lua.type(index) != lua_nil) {
    invalid_board("Board actor is unavailable");
  }
  lua.restore(index - 1);
}

void read_items(const LuaView &lua, int cell, int face, int row, int col,
                int inspiration, Json &objects) {
  const auto items = lua.field(cell, "item");
  if (lua.type(items) == lua_table) {
    lua.api.push_nil(lua.state);
    for (int count = 0; lua.api.next(lua.state, items); ++count) {
      const auto item = lua.top();
      if (count >= 32 || lua.type(item) != lua_table)
        invalid_board("Unexpected board item table");
      const int id = lua.number(item, "id").value_or(0);
      const int quantity = lua.number(item, "num").value_or(0);
      if (!id || !quantity)
        invalid_board("Missing board item ID or quantity");
      objects.push_back({{"type", id == inspiration ? "inspiration" : "item"},
                         {"face", face}, {"row", row}, {"col", col},
                         {"item_id", id}, {"quantity", quantity}});
      lua.restore(item - 1); // Keep the key for lua_next.
    }
  } else if (lua.type(items) != lua_nil) {
    invalid_board("Unexpected board item field");
  }
  lua.restore(items - 1);
}
std::optional<int> read_move_distance(const LuaView &lua, int model, int config) {
  const auto base = lua.number(config, "initialDistance");
  if (!base)
    return std::nullopt;
  const auto stack = lua.top();
  const auto manager = lua.field(model, "itemAttrManager");
  const auto globals = lua.at(registry, 2);
  const auto kinds = lua.type(globals) == lua_table ? lua.field(globals, "Enum_RogueItemOrderType") : 0;
  const auto key = kinds && lua.type(kinds) == lua_table ? lua.field(kinds, "StepDistanceItem") : 0;
  const auto bonus = key && lua.type(key) != lua_nil && lua.type(manager) == lua_table
                         ? lua.quota(manager, "GetAttr", key) : std::nullopt;
  lua.restore(stack);
  if (!bonus || *bonus > std::numeric_limits<int>::max() - *base)
    return std::nullopt;
  return *base + *bonus;
}

Json read_turn(const LuaView &lua, int model) {
  const auto config = lua.field(model, "cfg");
  const auto plane = lua.type(config) == lua_table ? lua.number(config, "faceIndex") : std::nullopt;
  const auto move_distance = plane ? read_move_distance(lua, model, config) : std::nullopt;
  lua.restore(config - 1);
  if (!plane || *plane < 1)
    throw Error("deep_dive_turn_unavailable", "Deep Dive plane index is unavailable");
  const auto rounds_used = lua.number(model, "stepNum");
  const auto moves = lua.number(model, "curMoveNum");
  const auto rotations = lua.number(model, "curRotNum");
  // These mirror RefreshStepNum and RefreshPlayerStep, including client bonuses.
  const auto total_rounds = lua.quota(model, "GetMaxRoundNum");
  const auto total_moves = lua.quota(model, "GetMaxStepNum");
  const auto total_rotations = lua.quota(model, "GetMaxRotNum");
  if (!rounds_used || !total_rounds || !moves || !rotations || !total_moves || !total_rotations)
    throw Error("deep_dive_turn_unavailable", "Deep Dive turn counters are unavailable");
  Json turn = {{"schema", "resonance_pc.deep_dive_turn.v1"},
          {"plane_index", *plane},
          {"rounds_remaining", std::max(*total_rounds - *rounds_used, 0)},
          {"rounds_used", *rounds_used}, {"rounds_total", *total_rounds},
          {"moves_used", *moves}, {"moves_total", *total_moves},
          {"moves_left", std::max(*total_moves - *moves, 0)},
          {"rotations_used", *rotations}, {"rotations_total", *total_rotations},
          {"rotations_left", std::max(*total_rotations - *rotations, 0)}};
  if (move_distance)
    turn["move_distance"] = *move_distance;
  return turn;
}

// Call the installed client's UI handlers. The bridge supplies checked native
// coordinates and uses the same confirmation path as a player using the UI.
constexpr auto action_handler = R"lua(
local model, op, face, row, col, move_distance = ...
local prefix = 'UICubeRogueMain/'
local controller = package.loaded[prefix .. 'UICubeRoguewMainController']
local view = package.loaded[prefix .. 'UICubeRogueMainView']
local buttons = package.loaded[prefix .. 'UICubeRogueMainViewFunction']
if not controller or not view or not buttons then return 'waiting' end

local function visible(widget)
    if type(widget) == 'table' then widget = widget.self end
    return widget and not widget:IsNull() and widget.gameObject.activeInHierarchy
end
local function matches(cell)
    return cell and cell.face == face and cell.row == row and cell.col == col
end
local function press(name)
    buttons['CubeRogueMain_' .. name .. '_Click'](nil, nil)
end

if not visible(view.Group_ActionInformation) or not visible(view.Group_MoveTips.Group_PlayerMove)
    or visible(view.Group_MoveTips.Group_BossMove) then return 'waiting', 'player_controls_inactive' end
if visible(view.Group_EventOptions) or visible(view.Group_RestArea) or visible(view.Group_Enter) then
    return 'waiting', 'board_overlay_active'
end
-- needTriggerRoundEnd is only consulted by the client while needStartRound is
-- set. The round-start callback clears needStartRound and can leave it set.
if model.needStartRound or model.bossRotating or not controller.CanDealRotStuff() then
    return 'waiting', 'round_transition_or_animation'
end
local card = visible(view.Group_EventDetails)
local preview = visible(view.Group_ConfirmRot)
if op == 'confirm_move' then
    if not card or not visible(view.Group_EventDetails.Btn_Fight)
        or not controller.playerMoving or model.hasConfirmRot ~= true
        or not matches(model.curSelectCube) or not model.CanMoveToCube(face, row, col) then
        return 'waiting', 'move_confirmation_not_ready'
    end
    press('Group_EventDetails_Btn_Fight')
    return 'submitted'
end
if op == 'confirm_rotate' then
    if card or not preview or model.hasConfirmRot ~= false
        or not visible(view.Group_ConfirmRot.Group_Confirm.Btn_) then return 'waiting', 'rotation_confirmation_not_ready' end
    if model.rubikCube.GetRotateNextFace(model.roleCtrl.face, model.isHorizon, model.clockwise) ~= face then
        return 'preview_changed'
    end
    press('Group_ConfirmRot_Group_Confirm_Btn_')
    return model.hasConfirmRot and 'submitted' or 'waiting'
end
if card or preview or model.hasConfirmRot ~= true then return 'waiting', 'unconfirmed_selection' end
local move_button = 'Group_ActionInformation_Group_Move_Btn_'
local spin_button = 'Group_ActionInformation_Group_Spin_Btn_'
if op == 'normalize' then
    if controller.playerMoving then
        press(move_button)
        return controller.playerMoving and 'waiting' or 'mode_changed'
    elseif controller.playerRotating then
        press(spin_button)
        return controller.playerRotating and 'waiting' or 'mode_changed'
    end
    return 'ready'
end
local actor = model.roleCtrl
if op == 'prepare_move' then
    if not controller.playerMoving then
        press(move_button)
        return controller.playerMoving and 'mode_changed' or 'waiting', 'move_mode_not_open'
    end
    local target
    for _,cell in pairs(model.canMoveCubes or {}) do
        local distance = math.abs(cell.row - actor.x) + math.abs(cell.col - actor.y)
        if cell.face ~= actor.face or distance < 1 or distance > move_distance then
            return 'unsupported_move_options'
        end
        if matches(cell) then target = cell end
    end
    if not target or not model.CanMoveToCube(face, row, col) then return 'waiting', 'move_target_not_available' end
    local events = CS.UnityEngine.EventSystems
    local event_system = events.EventSystem.current
    if not event_system or event_system:IsNull() then return 'waiting' end
    local event = events.PointerEventData(event_system)
    -- The down handler clears the client's own drag marker. No mouse screen
    -- coordinates are used to select the target (the registered cell is passed in).
    controller.OnCubePointerDown(event, target)
    controller.OnCubePointerUp(event, target)
    return matches(model.curSelectCube) and 'move_selected' or 'waiting', 'native_move_selection_not_applied'
end
if not controller.playerRotating then
    press(spin_button)
    return controller.playerRotating and 'mode_changed' or 'waiting', 'rotation_mode_not_open'
end
local arrows = actor.rotBtns
if not arrows or arrows:IsNull() or not arrows.activeInHierarchy then return 'waiting', 'rotation_controls_inactive' end
for _, direction in ipairs({{'forward', 1, -1}, {'back', 1, 1}, {'left', -1, -1}, {'right', -1, 1}}) do
    local name, vertical, clockwise = direction[1], direction[2], direction[3]
    local button = arrows.transform:Find(name)
    if button and not button:IsNull() and button.gameObject.activeInHierarchy
        and model.rubikCube.GetRotateNextFace(actor.face, vertical == -1, clockwise) == face then
        controller.OnPlayerRotBtnClick(vertical, clockwise)
        return model.hasConfirmRot == false and 'rotation_previewed' or 'waiting', 'native_rotation_preview_not_applied'
    end
end
return 'waiting', 'rotation_direction_not_available'
)lua";

Json perform_action(LuaState *state, const Json &args) {
  const auto op = args.at("command").get<std::string>();
  const bool move = op == "prepare_move" || op == "confirm_move";
  const bool rotate = op == "prepare_rotate" || op == "confirm_rotate";
  if (!move && !rotate && op != "normalize")
    throw Error("deep_dive_action_invalid", "Unknown Deep Dive action");
  const LuaView lua(lua_api(), state);
  const auto loaded = lua.field(registry, "_LOADED");
  const auto model = lua.type(loaded) == lua_table
                         ? lua.field(loaded, "UICubeRogueMain/UICubeRogueMainDataModel") : 0;
  const auto rubik = model && lua.type(model) == lua_table ? lua.field(model, "rubikCube") : 0;
  const int size = rubik && lua.type(rubik) == lua_table ? lua.number(rubik, "size").value_or(0) : 0;
  if (size < 3 || size > 5 || lua.type(lua.field(rubik, "obj")) != lua_userdata)
    throw Error("deep_dive_board_unavailable", "A loaded 3x3, 4x4 or 5x5 Deep Dive board is required");
  lua.restore(rubik);
  int move_distance = 0;
  if (move || rotate) {
    const auto map_id = lua.number(model, "mapID");
    const auto role = lua.field(model, "roleCtrl");
    const auto &origin = args.at("actor");
    const auto turn = read_turn(lua, model);
    const auto &expected = args.at("turn");
    move_distance = turn.value("move_distance", 0);
    if (move_distance < 1 || move_distance > 2 ||
        move_distance != args.value("move_distance", move_distance))
      throw Error("deep_dive_action_state_changed", "Deep Dive move distance is unavailable or changed before input");
    if (size != args.value("cube_size", size) ||
        !map_id || *map_id != args.at("map_id").get<int>() || lua.type(role) != lua_table ||
        lua.number(role, "face") != origin.at("face").get<int>() ||
        lua.number(role, "x") != origin.at("row").get<int>() ||
        lua.number(role, "y") != origin.at("col").get<int>())
      throw Error("deep_dive_action_state_changed", "The Deep Dive map or player changed before input");
    for (const char *key : {"plane_index", "rounds_remaining", "moves_used", "moves_total",
                           "rotations_used", "rotations_total"}) {
      if (turn.at(key) != expected.at(key))
        throw Error("deep_dive_action_state_changed", "Deep Dive turn changed before input");
    }
    if (turn.at(move ? "moves_left" : "rotations_left") == 0)
      throw Error("deep_dive_action_state_changed", "No remaining Deep Dive action quota");
    lua.restore(rubik);
  }
  const int face = args.value("face", 0);
  const int row = args.value("row", 0);
  const int col = args.value("col", 0);
  if (((move || rotate) && (face < 1 || face > face_count)) ||
      (move && (row < 1 || row > size || col < 1 || col > size)))
    throw Error("deep_dive_action_invalid", "Invalid native Deep Dive target");

  if (lua.api.load_string(lua.state, action_handler) != 0)
    throw Error("deep_dive_action_failed", "Could not load the Deep Dive UI handler");
  lua.api.push_value(lua.state, model);
  lua.api.push_string(lua.state, op.c_str());
  for (int value : {face, row, col, move_distance})
    lua.api.push_integer(lua.state, value);
  if (lua.api.protected_call(lua.state, 6, 2, 0, 0, nullptr) != 0) {
    size_t length{};
    const auto details = lua.api.string(lua.state, lua.top(), &length);
    throw Error("deep_dive_action_failed", details ? std::string(details, std::min(size_t(240), length))
                                                  : "The Deep Dive UI handler failed");
  }
  size_t length{};
  const auto status = lua.api.string(lua.state, lua.top() - 1, &length);
  if (!status || length > 40)
    throw Error("deep_dive_action_failed", "Missing Deep Dive action result");
  Json result{{"status", std::string(status, length)}};
  if (result["status"] == "waiting") {
    if (const auto reason = lua.api.string(lua.state, lua.top(), &length); reason && length <= 60)
      result["waiting_reason"] = std::string(reason, length);
  }
  return result;
}

Json perform_choice(LuaState *state, const Json &args) {
  const auto command = args.at("command").get<std::string>();
  const bool read = command == "read";
  if (!read && command != "select" && command != "confirm" && command != "discard")
    throw Error("deep_dive_choice_invalid", "Unknown Deep Dive choice command");
  const auto kind = read ? std::string{} : args.at("kind").get<std::string>();
  const int revision = read ? 0 : args.at("revision").get<int>();
  const int subject = read ? 0 : args.at("subject_id").get<int>();
  const int index = read ? 0 : args.at("index").get<int>();
  const int id = read ? 0 : args.at("id").get<int>();
  const bool selection = kind == "options" || kind == "cards";
  const bool direct_confirm = kind == "battle_defeat" || kind == "battle_victory" ||
                              kind == "rest_shortage" || kind == "rest_finish";
  const bool supported = selection ? command != "discard" :
                         (kind == "replacement" && command == "discard") ||
                         (direct_confirm && command == "confirm");
  if (!read && (revision < 1 || subject < 1 || index < 1 || index > 4 || id < 1 ||
                !supported))
    throw Error("deep_dive_choice_invalid", "Invalid Deep Dive choice target");

  const LuaView lua(lua_api(), state);
  constexpr auto key = "AuraBridge.DeepDiveChoice.v1";
  const auto tracking = lua.field(registry, key);
  if (lua.type(tracking) != lua_table) {
    lua.restore(tracking - 1);
    lua.api.create_table(state, 0, 5);
    lua.api.push_string(state, key);
    lua.api.push_value(state, tracking);
    lua.api.raw_set(state, registry);
  }
  static const auto script = std::string(deep_dive_choice_prelude) + deep_dive_reward_choice_script +
                             deep_dive_event_choice_script;
  if (lua.api.load_string(state, script.c_str()) != 0)
    throw Error("deep_dive_choice_failed", "Could not load the Deep Dive choice handler");
  lua.api.push_value(state, tracking);
  lua.api.push_string(state, command.c_str());
  lua.api.push_integer(state, revision);
  lua.api.push_string(state, kind.c_str());
  for (const int value : {subject, index, id})
    lua.api.push_integer(state, value);
  if (lua.api.protected_call(state, 7, 1, 0, 0, nullptr) != 0) {
    size_t size{};
    const auto error = lua.api.string(state, lua.top(), &size);
    throw Error("deep_dive_choice_failed", error ? std::string(error, std::min(size_t(240), size))
                                                  : "The Deep Dive choice handler failed");
  }

  const auto result = lua.top();
  if (lua.type(result) != lua_table)
    throw Error("deep_dive_choice_failed", "Missing Deep Dive choice result");
  const auto status = lua.text(result, "status");
  if (!read)
    return {{"status", status}};
  Json snapshot = {{"schema", "resonance_pc.deep_dive_choice.v1"}, {"status", status},
                   {"kind", lua.text(result, "kind")}};
  const auto reason = lua.text(result, "reason");
  if (!reason.empty())
    snapshot["reason"] = reason;
  if (status != "ready" && status != "submitted")
    return snapshot;
  const auto observed_revision = lua.number(result, "revision").value_or(0);
  const auto observed_subject = lua.number(result, "subject_id").value_or(0);
  const int rows = lua.field(result, "rows");
  const auto count = lua.type(rows) == lua_table ? lua.api.raw_length(state, rows) : 0;
  if (!observed_revision || !observed_subject || count == 0 || count > 4)
    throw Error("deep_dive_choice_failed", "Invalid Deep Dive choice list");
  snapshot["revision"] = observed_revision;
  snapshot["subject_id"] = observed_subject;
  snapshot["rows"] = Json::array();
  for (int position = 1; position <= int(count); ++position) {
    const auto row = lua.at(rows, position);
    if (lua.type(row) != lua_table || lua.number(row, "index") != position)
      throw Error("deep_dive_choice_failed", "Invalid Deep Dive choice row");
    const auto option_id = lua.number(row, "id").value_or(0);
    if (!option_id)
      throw Error("deep_dive_choice_failed", "Missing Deep Dive choice ID");
    snapshot["rows"].push_back({{"id", option_id}, {"index", position},
                                 {"disabled", lua.flag(row, "disabled")},
                                 {"selected", lua.flag(row, "selected")}});
    lua.restore(rows);
  }
  return snapshot;
}

// This fixed reader calls the same camera getter and face lookup as the UI.
// It projects the live selection controls in the Unity client viewport.
constexpr auto controls_reader = R"lua(
local model = ...
local actor, cube = model.roleCtrl, model.rubikCube
local controller = package.loaded['UICubeRogueMain/UICubeRoguewMainController']
local camera = controller.FindCamera()
local unity = CS.UnityEngine
local w, h = unity.Screen.width, unity.Screen.height
local result = { width = w, height = h, size = cube.size, face = actor.face, row = actor.x, col = actor.y,
                 moves = {}, rotations = {} }

local function point(transform)
    if not transform or transform:IsNull() or not transform.gameObject.activeInHierarchy then return end
    local p = camera:WorldToScreenPoint(transform.position)
    local x, y = math.floor(p.x + 0.5), math.floor(h - p.y + 0.5)
    if p.z <= 0 or x < 0 or x >= w or y < 0 or y >= h then return end
    return x, y
end

if model.hasConfirmRot == false then
    result.preview_face = cube.GetRotateNextFace(actor.face, model.isHorizon, model.clockwise)
    return result
end
local selected = model.curSelectCube
if selected then result.selected = selected end
for _, cell in ipairs(model.canMoveCubes or {}) do
    local x, y = point(cell.moveBtn)
    if x then
        result.moves[#result.moves + 1] = { face = cell.face, row = cell.row, col = cell.col, x = x, y = y }
    end
end
if actor.rotBtns and not actor.rotBtns:IsNull() and actor.rotBtns.activeInHierarchy then
    local buttons = actor.rotBtns.transform
    -- CubeRogueRoleCtrl assigns these exact arguments to the four children.
    for _, control in ipairs({{'forward', false, -1}, {'back', false, 1},
                              {'left', true, -1}, {'right', true, 1}}) do
        local name, horizontal, clockwise = control[1], control[2], control[3]
        local x, y = point(buttons:Find(name))
        if x then
            result.rotations[#result.rotations + 1] = { name = name, x = x, y = y,
                next_face = cube.GetRotateNextFace(actor.face, horizontal, clockwise) }
        end
    end
end
return result
)lua";

Json read_controls(const LuaView &lua, int model) {
  if (lua.api.load_string(lua.state, controls_reader) != 0)
    throw Error("deep_dive_controls_unavailable", "Could not load the Deep Dive controls reader");
  lua.api.push_value(lua.state, model);
  if (lua.api.protected_call(lua.state, 1, 1, 0, 0, nullptr) != 0) {
    size_t length{};
    const auto details = lua.api.string(lua.state, lua.top(), &length);
    throw Error("deep_dive_controls_unavailable",
                details ? std::string(details, std::min(size_t(240), length)) :
                          "Deep Dive controls are unavailable");
  }
  const int data = lua.top();
  const auto field = [&](int row, const char *name) {
    const auto value = lua.number(row, name);
    if (!value)
      throw Error("deep_dive_controls_unavailable", "Incomplete Deep Dive controls");
    return *value;
  };
  Json result = {{"schema", "resonance_pc.deep_dive_controls.v1"},
                 {"width", field(data, "width")}, {"height", field(data, "height")},
                 {"size", field(data, "size")},
                 {"player", {{"face", field(data, "face")}, {"row", field(data, "row")},
                             {"col", field(data, "col")}}},
                 {"turn", read_turn(lua, model)},
                 {"moves", Json::array()}, {"rotations", Json::array()}};
  if (lua.flag(model, "hasConfirmRot", true) == false) {
    result["preview_face"] = field(data, "preview_face");
    return result;
  }
  const int selected = lua.field(data, "selected");
  if (lua.type(selected) == lua_table)
    result["selected"] = {{"face", field(selected, "face")}, {"row", field(selected, "row")},
                          {"col", field(selected, "col")}};
  lua.restore(data);
  for (const auto key : {"moves", "rotations"}) {
    const int list = lua.field(data, key);
    if (lua.type(list) != lua_table || lua.api.raw_length(lua.state, list) > 32)
      throw Error("deep_dive_controls_unavailable", "Invalid Deep Dive control list");
    const auto count = lua.api.raw_length(lua.state, list);
    for (size_t i = 1; i <= count; ++i) {
      const int item = lua.at(list, int(i));
      Json row = {{"point", {field(item, "x"), field(item, "y")}}};
      if (std::string_view(key) == "moves") {
        row.update({{"face", field(item, "face")}, {"row", field(item, "row")},
                    {"col", field(item, "col")}});
      } else {
        row.update({{"name", lua.text(item, "name")}, {"next_face", field(item, "next_face")}});
      }
      result[key].push_back(std::move(row));
      lua.restore(list);
    }
    lua.restore(data);
  }
  return result;
}

Json read_snapshot(LuaState *state, bool turn_only = false, bool controls_only = false) {
  const LuaView lua(lua_api(), state);
  const auto modules = lua.field(registry, "_LOADED");
  const auto model = lua.type(modules) == lua_table
                         ? lua.field(modules, "UICubeRogueMain/UICubeRogueMainDataModel") : 0;
  const auto rubik = model && lua.type(model) == lua_table ? lua.field(model, "rubikCube") : 0;
  if (!rubik || lua.type(rubik) != lua_table || lua.type(lua.field(rubik, "obj")) != lua_userdata)
    throw Error("deep_dive_board_unavailable", "Open the Deep Dive board before reading a snapshot");
  lua.restore(rubik);
  if (turn_only)
    return read_turn(lua, model);
  if (lua.flag(model, "isRotating") || lua.flag(model, "isResetRoting") ||
      lua.flag(model, "playerMoving") || lua.flag(model, "bossRotating"))
    throw Error("deep_dive_board_busy", "The Deep Dive board is in transition");
  if (controls_only)
    return read_controls(lua, model);
  if (!lua.flag(model, "hasConfirmRot", true))
    throw Error("deep_dive_board_busy", "The Deep Dive board has a pending preview");
  const int size = lua.number(rubik, "size").value_or(0);
  // The client decodes single-digit row/column suffixes in its slot names.
  if (size < 1 || size > 9)
    invalid_board("Unsupported Deep Dive board size");
  const auto cells = lua.field(rubik, "cubeData");
  if (!lua.array(cells, face_count))
    invalid_board("Expected six Deep Dive faces");

  const auto config = lua.field(model, "allConfig");
  const int inspiration = lua.type(config) == lua_table ? lua.number(config, "specialItemId").value_or(0) : 0;
  const auto globals = lua.at(registry, 2);
  const int cache = lua.type(globals) == lua_table ? lua.field(globals, "_dataCache") : 0;
  const int events = cache && lua.type(cache) == lua_table ? lua.field(cache, "ActivityListFactory") : 0;
  const int tags = cache && lua.type(cache) == lua_table ? lua.field(cache, "TagFactory") : 0;
  Json board = {{"schema", "resonance_pc.deep_dive_board.v1"}, {"size", size}, {"coordinate_base", 1},
                {"faces", Json::array()}, {"objects", Json::array()}, {"node_types", Json::object()},
                {"turn", read_turn(lua, model)}};
  if (const auto map = lua.number(model, "mapID"))
    board["map_id"] = *map;
  auto &objects = board["objects"];
  read_actor(lua, model, "roleCtrl", "player", size, objects, true);
  read_actor(lua, model, "bossCtrl", "singularity", size, objects);

  for (int face = 1; face <= face_count; ++face) {
    const auto face_index = lua.at(cells, face);
    if (!lua.array(face_index, size))
      invalid_board("Incomplete Deep Dive face");
    Json rows = Json::array();
    for (int row = 1; row <= size; ++row) {
      const auto row_index = lua.at(face_index, row);
      if (!lua.array(row_index, size))
        invalid_board("Incomplete Deep Dive row");
      Json nodes = Json::array();
      for (int col = 1; col <= size; ++col) {
        const auto cell = lua.at(row_index, col);
        if (lua.type(cell) != lua_table)
          invalid_board("Missing Deep Dive node");
        const auto net = lua.field(cell, "netData");
        const auto event = lua.number(net);
        if (!event && lua.type(net) != lua_nil)
          invalid_board("Unexpected Deep Dive event template");
        const int event_id = event.value_or(0);
        Json node = {{"type", "empty"}};
        if (event_id) {
          const auto record = cache ? cached_record(lua, cache, events, event_id) : 0;
          const int kind = record && lua.type(record) == lua_table ? lua.number(record, "typeId").value_or(0) : 0;
          node = {{"type", node_kind(kind)}, {"event_template_id", event_id}};
          if (kind) {
            node["type_id"] = kind;
            const auto key = std::to_string(kind);
            if (!board["node_types"].contains(key)) {
              const auto tag = cached_record(lua, cache, tags, kind);
              const auto name = tag && lua.type(tag) == lua_table ? lua.text(tag, "eventTypeName") : std::string{};
              board["node_types"][key] = {{"type", node_kind(kind)}, {"name", name}};
            }
          }
        }
        lua.restore(cell);
        if (const auto id = lua.number(cell, "realId"))
          node["event_id"] = *id;
        read_items(lua, cell, face, row, col, inspiration, objects);
        nodes.push_back(std::move(node));
        lua.restore(row_index);
      }
      rows.push_back(std::move(nodes));
      lua.restore(face_index);
    }
    board["faces"].push_back({{"face", face}, {"rows", std::move(rows)}});
    lua.restore(face_index - 1);
  }
  // Leave room for the standard reply envelope within the 64 KiB bridge limit.
  if (board.dump().size() > 63 * 1024)
    throw Error("deep_dive_board_too_large", "The board snapshot exceeds the bridge message limit");
  return board;
}
} // namespace

bool deep_dive_board_available() {
  try {
    lua_api();
    return true;
  } catch (const Error &) {
    return false;
  }
}

nlohmann::json read_deep_dive_board(bool turn_only, bool controls_only) {
  auto state = current_lua_state();
  if (!state)
    throw Error("deep_dive_board_unavailable", "The game Lua state is unavailable");
  return read_snapshot(state, turn_only, controls_only);
}

nlohmann::json execute_deep_dive_action(const nlohmann::json &args) {
  const auto state = current_lua_state();
  if (!state)
    throw Error("deep_dive_board_unavailable", "The game Lua state is unavailable");
  return perform_action(state, args);
}

nlohmann::json execute_deep_dive_choice(const nlohmann::json &args) {
  const auto state = current_lua_state();
  if (!state) {
    // The client can replace its Lua environment during battle exit.
    if (args.at("command") == "read")
      return {{"schema", "resonance_pc.deep_dive_choice.v1"}, {"status", "waiting"}, {"kind", "none"}};
    return {{"status", "waiting"}};
  }
  return perform_choice(state, args);
}
} // namespace bridge
