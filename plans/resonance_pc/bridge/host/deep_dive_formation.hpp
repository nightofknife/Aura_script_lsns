#pragma once

namespace bridge {

inline constexpr auto deep_dive_formation_script = R"lua(
local formation_url = 'UI/CubeRogue/Team_Cube/Team_Battle'
local main_url = 'UI/CubeRogue/Main/CubeRogueMain'
local quit_url = 'UI/CubeRogue/Quit/CubeRogueQuit'
local main_controller = 'UICubeRogueMain/UICubeRoguewMainController'

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

local function read_formation(url)
    if url ~= formation_url then return nil end
    local prefix = 'UITeam_Battle/UITeam_Battle'
    local model = package.loaded[prefix .. 'DataModel']
    if not model or model.mode ~= 'CubeSquad' then return nil end
    local panel = {kind='formation_start', ready=false, rows={}}
    local view = package.loaded[prefix .. 'View']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local owner = UIManager:GetPanel(url)
    local level = reward_choice_id(model.levelId)
    local slots = model.levelRoleList
    if not owner or owner.IsPlayAnimator or not visible(view) or not buttons or not level
        or model.curLevelIndex ~= 1 or model.isDrag or type(slots) ~= 'table' or #slots < 1 then return panel end

    local run, roster, available = formation_roster()
    if not run then return panel end
    local selected, count = {}, 0
    for slot=1,#slots do
        if slots[slot] ~= -1 then
            local id = reward_choice_id(slots[slot])
            if not id or not roster[id] or selected[id] then return panel end
            selected[id] = true
            count = count + 1
        end
    end
    panel.selected_count, panel.available_count, panel.required_count = count, available, #slots
    if count == #slots and visible(view.Btn_Battle) and type(buttons.Team_Battle_Btn_Battle_Click) == 'function' then
        panel.confirm = buttons.Team_Battle_Btn_Battle_Click
        return formation_control(panel, 'formation_start', view.self, level, level)
    end
    if available < #slots then
        local board = formation_board()
        local back = buttons.Team_Battle_Btn_Back_Click
        if not board or not visible(view.Btn_Back) or type(back) ~= 'function' then return panel end
        panel.confirm = function()
            back(nil, nil)
            -- Saving the deck can replace CubeRogueData while the current board persists.
            tracking.formation_exit = {board=board, level=level, required=#slots}
        end
        return formation_control(panel, 'formation_leave', view.self, level, level)
    end

    local click = buttons.Team_Battle_ScrollGrid_RoleList_Group_RoleFace_Btn_Click_Click
    if not visible(view.ScrollGrid_RoleList) or type(click) ~= 'function'
        or type(model.Roles) ~= 'table' or type(model.roleCheckMap) ~= 'table' then return panel end
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
    if not candidate then return panel end
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
    if not run then return panel end
    if formation_board() ~= exit.board or available >= exit.required then
        tracking.formation_exit = nil
        return nil
    end
    local owner = UIManager:GetPanel(url)
    if not owner or owner.IsPlayAnimator then return panel end
    if url == main_url then
        local view = package.loaded['UICubeRogueMain/UICubeRogueMainView']
        local controller = package.loaded[main_controller]
        if not visible(view) or not controller or not controller.CanDealRotStuff() then return panel end
        -- Open the same quit panel as the main return button, including when an event owns the board.
        panel.confirm = function() UIManager:Open(quit_url) end
        return formation_control(panel, 'formation_quit', view.self, exit.level, exit.level)
    end
    local prefix = 'UICubeRogueQuit/UICubeRogueQuit'
    local view = package.loaded[prefix .. 'View']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local finish = buttons and buttons.CubeRogueQuit_Group_Btn_Btn_Finish_Click
    if not view or not view.Group_Btn or not visible(view.Group_Btn.Btn_Finish)
        or type(finish) ~= 'function' then return panel end
    panel.confirm = function()
        finish(nil, nil)
        tracking.formation_exit = nil
    end
    return formation_control(panel, 'formation_finish', view.self, exit.level, exit.level)
end
)lua";

} // namespace bridge
