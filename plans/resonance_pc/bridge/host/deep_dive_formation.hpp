#pragma once

namespace bridge {

inline constexpr auto deep_dive_formation_script = R"lua(
local formation_url = 'UI/CubeRogue/Team_Cube/Team_Battle'
local main_url = 'UI/CubeRogue/Main/CubeRogueMain'
local quit_url = 'UI/CubeRogue/Quit/CubeRogueQuit'
local main_controller = 'UICubeRogueMain/UICubeRoguewMainController'
local formation_size = 5

local function formation_board()
    local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    return model and model.rubikCube
end

local function formation_roster()
    local cache = PlayerData and PlayerData.TempCache
    local run = cache and cache.CubeRogueData
    local rogue = package.loaded[main_controller]
    if not run or type(run.roles) ~= 'table' or not rogue
        or type(rogue.GetRoleHpPercent) ~= 'function' then return nil end
    local available, count = {}, 0
    for key, role in pairs(run.roles) do
        local id = reward_choice_id(key)
        if not id or type(role) ~= 'table' or available[id] ~= nil then return nil end
        local hp = rogue:GetRoleHpPercent(tostring(id))
        if type(hp) ~= 'number' then return nil end
        available[id] = hp > 0
        if available[id] then count = count + 1 end
    end
    return run, available, count
end

local function formation_control(panel, kind, identity, subject, target)
    panel.kind, panel.identity, panel.subject_id = kind, identity, subject
    panel.rows = {{id=target, index=1, disabled=false, selected=false}}
    panel.ready = true
    return panel
end

local function formation_wait(panel, reason)
    panel.wait_reason = reason
    return panel
end

local function formation_entry_finished(owner)
    local animator = owner:GetComponent(typeof(CS.UnityEngine.Animator))
    if not animator or animator:IsNull() or not animator.isActiveAndEnabled
        or animator.layerCount ~= 1 or animator:IsInTransition(0) then return false end
    local state = animator:GetCurrentAnimatorStateInfo(0)
    -- The page requests Animation_SelectOpen, but its controller only has Team_Battle.
    return state:IsName('Base Layer.Team_Battle') and not state.loop and state.normalizedTime >= 1
end

local function read_formation(url)
    if url ~= formation_url then return nil end
    local prefix = 'UITeam_Battle/UITeam_Battle'
    local panel = {kind='formation_start', ready=false, rows={}}
    local model = package.loaded[prefix .. 'DataModel']
    if not model or not model.mode then return formation_wait(panel, 'formation_model_unavailable') end
    if model.mode ~= 'CubeSquad' then return nil end
    local owner = UIManager:GetPanel(url)
    if not owner then return formation_wait(panel, 'formation_owner_unavailable') end
    local view = package.loaded[prefix .. 'View']
    if not visible(view) then return formation_wait(panel, 'formation_view_hidden') end
    local level = reward_choice_id(model.levelId)
    if not level then return formation_wait(panel, 'formation_level_unavailable') end
    panel.subject_id, panel.required_count = level, formation_size
    if model.curLevelIndex ~= 1 then return formation_wait(panel, 'formation_level_tab_inactive') end
    if model.isDrag then return formation_wait(panel, 'formation_drag_active') end
    local slots = model.levelRoleList
    if type(slots) ~= 'table' or #slots ~= formation_size then
        return formation_wait(panel, 'formation_slots_invalid')
    end

    local selected, count = {}, 0
    for slot=1,formation_size do
        if slots[slot] ~= -1 then
            local id = reward_choice_id(slots[slot])
            if not id or selected[id] then return formation_wait(panel, 'formation_slots_invalid') end
            selected[id] = true
            count = count + 1
        end
    end
    panel.selected_count = count
    if owner.IsPlayAnimator and not formation_entry_finished(owner) then
        return formation_wait(panel, 'formation_animating')
    end
    local buttons = package.loaded[prefix .. 'ViewFunction']
    -- The native start callback validates occupied slots and saves any deck changes.
    -- Roster data is only needed to choose how to handle open slots.
    if count == formation_size then
        local confirm = buttons and buttons.Team_Battle_Btn_Battle_Click
        if type(confirm) ~= 'function' then return formation_wait(panel, 'formation_start_callback_missing') end
        if not visible(view.Btn_Battle) then return formation_wait(panel, 'formation_start_button_hidden') end
        local widget = view.Btn_Battle
        if type(widget) == 'table' then widget = widget.self end
        local button = widget.Btn
        if not button or button:IsNull() or not button.isActiveAndEnabled or not button:IsInteractable() then
            return formation_wait(panel, 'formation_start_button_disabled')
        end
        panel.confirm = confirm
        return formation_control(panel, 'formation_start', view.self, level, level)
    end

    local run, roster, available = formation_roster()
    if not run then return formation_wait(panel, 'formation_roster_unavailable') end
    panel.available_count = available
    for id in pairs(selected) do
        if not roster[id] then return formation_wait(panel, 'formation_selected_role_unavailable') end
    end
    if available < formation_size then
        local board = formation_board()
        local back = buttons and buttons.Team_Battle_Btn_Back_Click
        if not board or not visible(view.Btn_Back) or type(back) ~= 'function' then
            return formation_wait(panel, 'formation_back_unavailable')
        end
        panel.confirm = function()
            back(nil, nil)
            -- Saving the deck can replace CubeRogueData while the current board persists.
            tracking.formation_exit = {board=board, level=level, required=formation_size}
        end
        return formation_control(panel, 'formation_leave', view.self, level, level)
    end

    local click = buttons and buttons.Team_Battle_ScrollGrid_RoleList_Group_RoleFace_Btn_Click_Click
    if not visible(view.ScrollGrid_RoleList) or type(click) ~= 'function'
        or type(model.Roles) ~= 'table' or type(model.roleCheckMap) ~= 'table' then
        return formation_wait(panel, 'formation_recruit_controls_unavailable')
    end
    local function eligible(id)
        return id and roster[id] and not selected[id] and not model.roleCheckMap[tostring(id)]
    end
    local candidate
    for _, role in ipairs(model.Roles) do
        local id = reward_choice_id(role)
        if eligible(id) then candidate = id; break end
    end
    -- The client's filters may hide a recruit from the scroll list.
    if not candidate then
        for id in pairs(roster) do
            if eligible(id) and (not candidate or id < candidate) then candidate = id end
        end
    end
    if not candidate then return formation_wait(panel, 'formation_recruit_unavailable') end
    panel.fill = function()
        click(nil, tostring(candidate))
        for _, role in ipairs(model.levelRoleList) do
            if reward_choice_id(role) == candidate then return 'applied' end
        end
        return 'waiting'
    end
    return formation_control(panel, 'formation_fill', view.self, level, candidate)
end

local function read_formation_exit(url)
    local exit = tracking.formation_exit
    if not exit or (url ~= main_url and url ~= quit_url) then return nil end
    local panel = {kind=url == main_url and 'formation_quit' or 'formation_finish', ready=false, rows={}}
    local run, _, available = formation_roster()
    if not run then return formation_wait(panel, 'formation_roster_unavailable') end
    if formation_board() ~= exit.board or available >= exit.required then
        tracking.formation_exit = nil
        return nil
    end
    local owner = UIManager:GetPanel(url)
    if not owner then return formation_wait(panel, 'formation_owner_unavailable') end
    if owner.IsPlayAnimator then return formation_wait(panel, 'formation_animating') end
    if url == main_url then
        local view = package.loaded['UICubeRogueMain/UICubeRogueMainView']
        local controller = package.loaded[main_controller]
        if not visible(view) or not controller or not controller.CanDealRotStuff() then
            return formation_wait(panel, 'formation_quit_unavailable')
        end
        -- Open the same quit panel as the main return button, including when an event owns the board.
        panel.confirm = function() UIManager:Open(quit_url) end
        return formation_control(panel, 'formation_quit', view.self, exit.level, exit.level)
    end
    local prefix = 'UICubeRogueQuit/UICubeRogueQuit'
    local view = package.loaded[prefix .. 'View']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local finish = buttons and buttons.CubeRogueQuit_Group_Btn_Btn_Finish_Click
    if not view or not view.Group_Btn or not visible(view.Group_Btn.Btn_Finish)
        or type(finish) ~= 'function' then return formation_wait(panel, 'formation_finish_unavailable') end
    panel.confirm = function()
        finish(nil, nil)
        tracking.formation_exit = nil
    end
    return formation_control(panel, 'formation_finish', view.self, exit.level, exit.level)
end
)lua";

} // namespace bridge
