#pragma once

#include "deep_dive_reward_choices.hpp"

namespace bridge {

inline constexpr auto deep_dive_choice_prelude = R"lua(
local tracking, command, revision, kind, subject, index, id = ...

local function visible(widget)
    if type(widget) == 'table' then widget = widget.self end
    return widget and not widget:IsNull() and widget.gameObject.activeInHierarchy
end

local function network_busy()
    local mask = UIManager and UIManager:GetPanel('UI/Common/NetMask')
    return mask and mask.IsActive
end
)lua";

inline constexpr auto deep_dive_event_choice_script = R"lua(
local function read_event_choice(url)
    if url ~= 'UI/CubeRogue/Main/CubeRogueMain' then return nil end
    local prefix = 'UICubeRogueMain/'
    local view = package.loaded[prefix .. 'UICubeRogueMainView']
    local page = view and view.Group_EventOptions
    if not visible(page) or not visible(page.Group_Options) then return nil end

    local panel = {kind='options', ready=false, rows={}}
    local controller = package.loaded[prefix .. 'UICubeRoguewMainController']
    local model = package.loaded[prefix .. 'UICubeRogueMainDataModel']
    if not controller or not model then return panel end
    local event = controller.curOptionEvent
    local choices = model.AllOptions
    if not event or type(choices) ~= 'table' then return panel end
    panel.subject_id = reward_choice_id(event.id)
    if not panel.subject_id then return panel end
    panel.identity = choices

    if controller.optionPanelState ~= 4 or model.isDealingOrder
        or type(controller.OptionSelectClick) ~= 'function'
        or type(controller.OptionConfirmClick) ~= 'function' then return panel end
    if #choices < 1 or #choices > 4 then
        panel.reason = 'unsupported_event_option_count'
        return panel
    end
    for display_index,row in ipairs(choices) do
        local option_id = row.cfg and reward_choice_id(row.cfg.id)
        local native_index = reward_choice_id(row.index)
        if not option_id or not native_index then return panel end
        panel.rows[display_index] = {
            id=option_id, index=display_index, native_index=native_index,
            disabled=not not row.cannotSel, selected=controller.curSelectOption == native_index
        }
    end
    panel.controller = controller
    panel.ready = true
    return panel
end

local function read_battle_result(url)
    if url ~= 'UI/Settlement/Settlement' then return nil end
    local prefix = 'UISettlement/UISettlement'
    local view = package.loaded[prefix .. 'View']
    if not view then return nil end
    local victory, defeat = visible(view.Group_CubeVictory), visible(view.Group_CubeDefeat)
    if victory == defeat or not (victory or defeat) then return nil end
    local group = victory and view.Group_CubeVictory or view.Group_CubeDefeat
    local kind = victory and 'battle_victory' or 'battle_defeat'
    local panel = {kind=kind, ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    local model = package.loaded[prefix .. 'DataModel']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local level = model and model.LevelCA and reward_choice_id(model.LevelCA.id)
    local handler = victory and 'Settlement_Group_CubeVictory_Btn_Next_Click'
        or 'Settlement_Group_CubeDefeat_Btn_Next_Click'
    local confirm = buttons and buttons[handler]
    if not owner or owner.IsPlayAnimator or not level or not visible(group.Btn_Next)
        or type(confirm) ~= 'function' then
        return panel
    end
    panel.identity = group.self
    panel.subject_id = level
    panel.rows = {{id=level, index=1, disabled=false, selected=false}}
    panel.confirm = confirm
    panel.ready = true
    return panel
end

local function read_rest_settlement(url)
    local main = url == 'UI/CubeRogue/Main/CubeRogueMain'
    local quit = url == 'UI/CubeRogue/Quit/CubeRogueQuit'
    if not (main or quit) then return nil end
    local cache = PlayerData and PlayerData.TempCache
    local run = cache and cache.CubeRogueData
    local rest_id = run and reward_choice_id(run.rest_id)
    if not rest_id or type(run.roles) ~= 'table' then return nil end
    local prefix = 'UICubeRogueMain/UICubeRogueMain'
    local view = package.loaded[prefix .. 'View']
    local rest = view and view.Group_RestArea
    if not rest or not rest.self or rest.self:IsNull() or not rest.self.gameObject.activeSelf then
        return nil
    end

    -- Match the client's Continue check, including roster entries with full HP
    -- represented by a missing hp field.
    local available = 0
    for _,role in pairs(run.roles) do
        if role and (role.hp == nil or role.hp > 0) then available = available + 1 end
    end
    if available >= 5 then return nil end

    local panel = {kind=main and 'rest_shortage' or 'rest_finish', ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    if not owner or owner.IsPlayAnimator then return panel end
    if main then
        local buttons = package.loaded[prefix .. 'ViewFunction']
        local controller = package.loaded['UICubeRogueMain/UICubeRoguewMainController']
        if not visible(rest) or visible(view.Group_EventOptions) or not controller
            or not controller.CanDealRotStuff() then return panel end
        panel.identity = rest.self
        panel.confirm = buttons and buttons.CubeRogueMain_Group_CommonTopLeft_Btn_Return_Click
    else
        local quit_prefix = 'UICubeRogueQuit/UICubeRogueQuit'
        local quit_view = package.loaded[quit_prefix .. 'View']
        local buttons = package.loaded[quit_prefix .. 'ViewFunction']
        if not quit_view or not quit_view.Group_Btn or not visible(quit_view.Group_Btn.Btn_Finish) then
            return panel
        end
        panel.identity = quit_view.self
        panel.confirm = buttons and buttons.CubeRogueQuit_Group_Btn_Btn_Finish_Click
    end
    panel.subject_id = rest_id
    panel.rows = {{id=rest_id, index=1, disabled=false, selected=false}}
    panel.ready = panel.identity and type(panel.confirm) == 'function'
    return panel
end

if network_busy() then
    if command ~= 'read' then return {status='waiting'} end
    return {status='waiting', kind=tracking.identity and tracking.kind or 'none',
            wait_reason='network_busy'}
end

local url = UIManager and UIManager:GetCurrentPanelURL()
local panel = read_formation_exit(url) or read_formation(url) or read_reward_choice(url)
    or read_event_choice(url) or read_battle_result(url) or read_rest_settlement(url)
if not panel then
    tracking.identity = nil
    if command == 'read' then return {status='absent', kind='none'} end
    return {status='changed'}
end

-- Keep the original native list or callback alive for identity comparison.
-- Only a revision number crosses the bridge. New panels with identical
-- choices still have their own revision and one-shot confirmation.
if panel.identity and (tracking.identity ~= panel.identity or tracking.kind ~= panel.kind
        or tracking.subject ~= panel.subject_id) then
    tracking.revision = (tracking.revision or 0) + 1
    tracking.identity = panel.identity
    tracking.kind = panel.kind
    tracking.subject = panel.subject_id
    tracking.submitted = false
end

if command == 'read' then
    if not panel.ready then
        return {status='waiting', kind=panel.kind, reason=panel.reason, wait_reason=panel.wait_reason,
                subject_id=panel.kind == 'formation_start' and panel.subject_id or nil,
                selected_count=panel.selected_count, available_count=panel.available_count,
                required_count=panel.required_count}
    end
    local status = tracking.submitted and 'submitted' or 'ready'
    return {status=status, kind=panel.kind, revision=tracking.revision,
            subject_id=panel.subject_id, rows=panel.rows, selected_count=panel.selected_count,
            available_count=panel.available_count, required_count=panel.required_count}
end
if panel.kind ~= kind or tracking.revision ~= revision or panel.subject_id ~= subject then
    return {status='changed'}
end
if not panel.ready or tracking.submitted then return {status='waiting'} end
local row = panel.rows[index]
if not row or row.index ~= index or row.id ~= id or row.disabled then return {status='changed'} end
local direct_confirm = kind == 'battle_defeat' or kind == 'battle_victory'
    or kind == 'rest_shortage' or kind == 'rest_finish'
    or kind == 'formation_fill' or kind == 'formation_start' or kind == 'formation_leave'
    or kind == 'formation_quit' or kind == 'formation_finish'
if command == 'confirm' and not direct_confirm and not row.selected then
    return {status='changed'}
end

local status = 'waiting'
if kind == 'options' then
    if command == 'select' then
        if not row.selected then panel.controller.OptionSelectClick(row.native_index) end
        if panel.controller.curSelectOption == row.native_index then status = 'applied' end
    elseif command == 'confirm' then
        panel.controller.OptionConfirmClick(index)
        status = 'submitted'
    end
elseif kind == 'formation_fill' and command == 'confirm' then
    status = panel.fill()
elseif direct_confirm and command == 'confirm' then
    panel.confirm(nil, nil)
    status = 'submitted'
else
    status = apply_reward_choice(panel, command, row)
end
if status == 'submitted' then tracking.submitted = true end
return {status=status}
)lua";

} // namespace bridge
