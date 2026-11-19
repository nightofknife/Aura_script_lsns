#pragma once

namespace bridge {

// Opt-in diagnostics. Keep protocol arguments and callback return values intact.
inline constexpr auto deep_dive_event_trace_script = R"lua(
local mode = ...
local registry = debug.getregistry()
local key = 'AuraBridge.DeepDiveEventTrace.v1'
local trace = registry[key]

local function detach()
    if trace and trace.net.SendProto == trace.wrapper then
        trace.net.SendProto = trace.original
    end
    registry[key] = nil
end

if mode == 'stop' then
    detach()
    return {status='stopped'}
end

local net = Net
if type(net) ~= 'table' or type(net.SendProto) ~= 'function' then
    return {status='unsupported_net'}
end
if not trace then
    trace = {net=net, original=net.SendProto, records={}, sequence=0}
    local function record(stage, request, proto, coord, event_id)
        trace.sequence = trace.sequence + 1
        local records = trace.records
        if #records == 24 then table.remove(records, 1) end
        records[#records + 1] = {sequence=trace.sequence, stage=stage, request=request,
                                  protocol=proto, coord=coord, event_id=event_id}
        return trace.sequence
    end
    trace.wrapper = function(owner, proto, ...)
        if os.time() > trace.expires and registry[key] == trace then detach() end
        if registry[key] ~= trace or (proto ~= 'cube.move' and proto ~= 'cube.event') then
            return trace.original(owner, proto, ...)
        end
        local callback, param = ...
        local coord = type(param) == 'string' and param:match('^%d+:%d+:%d+$')
        local request = record('send', nil, proto, coord)
        if type(callback) ~= 'function' then
            return trace.original(owner, proto, ...)
        end
        local function observe_reply(...)
            local reply = ...
            local event_id = type(reply) == 'table' and tonumber(reply.event_id)
            record('reply', request, proto, coord, event_id)
            local result = table.pack(callback(...))
            record('callback_returned', request, proto, coord, event_id)
            return table.unpack(result, 1, result.n)
        end
        return trace.original(owner, proto, observe_reply, select(2, ...))
    end
    trace.expires = os.time() + 120
    net.SendProto = trace.wrapper
    registry[key] = trace
elseif trace.net ~= net or net.SendProto ~= trace.wrapper then
    return {status='net_handler_changed'}
end
trace.expires = os.time() + 120

local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
local controller = package.loaded['UICubeRogueMain/UICubeRoguewMainController']
local function cell_snapshot(cell)
    if type(cell) ~= 'table' then return nil end
    return {face=tonumber(cell.face), row=tonumber(cell.row), col=tonumber(cell.col),
            template_id=tonumber(cell.netData), event_id=tonumber(cell.realId)}
end
local panel = controller and controller.curOptionEvent
local result = {status='active', records=trace.records}
trace.records = {}
if model then
    result.player = cell_snapshot(model.playerCube)
    result.selected = cell_snapshot(model.curSelectCube)
    result.need_start_round = not not model.needStartRound
    result.round = tonumber(model.stepNum)
    result.moves = tonumber(model.curMoveNum)
    result.rotations = tonumber(model.curRotNum)
end
if controller then
    result.option_event_id = type(panel) == 'table' and tonumber(panel.id) or nil
    result.option_state = tonumber(controller.optionPanelState)
end
return result
)lua";

} // namespace bridge
