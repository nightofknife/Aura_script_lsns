#include "deep_dive_board.hpp"
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
  void (*push_nil)(LuaState *){};
  void (*push_value)(LuaState *, int){};
  int (*raw_get)(LuaState *, int){};
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
    LUA(push_nil, "pushnil");
    LUA(push_value, "pushvalue");
    LUA(raw_get, "rawget");
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
// Board fields use raw reads. Action totals use the client's two quota getters.
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
  std::optional<int> quota(int model, const char *getter) const {
    const auto stack = top();
    const auto function = field(model, getter);
    std::optional<int> result;
    if (type(function) == lua_function && api.protected_call(state, 0, 1, 0, 0, nullptr) == 0)
      result = number(top());
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
Json read_turn(const LuaView &lua, int model) {
  const auto rounds_used = lua.number(model, "stepNum");
  const auto moves = lua.number(model, "curMoveNum");
  const auto rotations = lua.number(model, "curRotNum");
  // These mirror RefreshStepNum and RefreshPlayerStep, including client bonuses.
  const auto total_rounds = lua.quota(model, "GetMaxRoundNum");
  const auto total_moves = lua.quota(model, "GetMaxStepNum");
  const auto total_rotations = lua.quota(model, "GetMaxRotNum");
  if (!rounds_used || !total_rounds || !moves || !rotations || !total_moves || !total_rotations)
    throw Error("deep_dive_turn_unavailable", "Deep Dive turn counters are unavailable");
  return {{"schema", "resonance_pc.deep_dive_turn.v1"},
          {"rounds_remaining", std::max(*total_rounds - *rounds_used, 0)},
          {"rounds_used", *rounds_used}, {"rounds_total", *total_rounds},
          {"moves_used", *moves}, {"moves_total", *total_moves},
          {"moves_left", std::max(*total_moves - *moves, 0)},
          {"rotations_used", *rotations}, {"rotations_total", *total_rotations},
          {"rotations_left", std::max(*total_rotations - *rotations, 0)}};
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
} // namespace bridge
