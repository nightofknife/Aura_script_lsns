#pragma once

namespace bridge {

inline constexpr auto deep_dive_reward_choice_script = R"lua(
local reward_card_path = 'UI/CubeRogue/ItemSelection/ItemSelection'
local reward_replace_path = 'UI/CubeRogue/ItemReplace/RogueItemReplace'

local function reward_choice_id(value)
    if type(value) ~= 'number' and type(value) ~= 'string' then return nil end
    local id = tonumber(value)
    if id and id > 0 and id % 1 == 0 then return id end
end

local function read_reward_choice(url)
    if url ~= reward_replace_path then tracking.replacement = nil end
    if url ~= reward_card_path and url ~= reward_replace_path then return nil end
    local kind = url == reward_card_path and 'cards' or 'replacement'
    local panel = {kind=kind, ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    if not owner or owner.IsPlayAnimator then
        return panel
    end

    if kind == 'cards' then
        local view = package.loaded['UIItemSelection/UIItemSelectionView']
        local model = package.loaded['UIItemSelection/UIItemSelectionDataModel']
        local controller = package.loaded['UIItemSelection/UIItemSelectionController']
        local buttons = package.loaded['UIItemSelection/UIItemSelectionViewFunction']
        if not view or not visible(view) or not model or not controller or not buttons then return panel end
        local mode = model.showType
        local grid = (mode == 1 or mode == 2) and view.ScrollGrid_Weapon or view.ScrollGrid_Buff
        local confirm = view.Group_Btn and view.Group_Btn.Btn_Confirm
        local data, selected = model.dataList, model.curSelect
        local subject_id = model.cfg and reward_choice_id(model.cfg.id)
        if not subject_id or type(data) ~= 'table' or type(selected) ~= 'table'
            or not visible(grid) or not visible(confirm) then return panel end
        if (mode ~= 1 and mode ~= 2 and mode ~= 3 and mode ~= 4) or model.maxSelect ~= 1 then
            panel.reason = 'unsupported_card_selection_mode'
            return panel
        end
        if #data < 1 or #data > 3 then
            panel.reason = 'unsupported_reward_card_count'
            return panel
        end
        if #selected > 1 then return panel end
        local selected_id = #selected == 1 and reward_choice_id(selected[1]) or nil
        if #selected == 1 and not selected_id then return panel end

        local rows, seen = {}, {}
        for index=1,#data do
            local id = type(data[index]) == 'table' and reward_choice_id(data[index].id)
            if not id or seen[id] then return panel end
            seen[id] = true
            rows[index] = {id=id, index=index, disabled=false, selected=id == selected_id}
        end
        if selected_id and not seen[selected_id] then return panel end
        panel.identity = data
        panel.subject_id = subject_id
        panel.rows = rows
        panel.controller = controller
        panel.confirm = buttons.ItemSelection_Group_Btn_Btn_Confirm_Click
        panel.can_confirm = model.canConfirm == true
        panel.ready = type(controller.Select) == 'function' and type(panel.confirm) == 'function'
        return panel
    end

    local view = package.loaded['UIRogueItemReplace/UIRogueItemReplaceView']
    local buttons = package.loaded['UIRogueItemReplace/UIRogueItemReplaceViewFunction']
    local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    if not view or not visible(view) or not buttons or not visible(view.Btn_Del) or not model then
        return panel
    end
    local cache = PlayerData and PlayerData.TempCache
    local bag = cache and cache.CubeRogueData and cache.CubeRogueData.inside_item
    local index = reward_choice_id(model.surplusItems)
    if not index or type(bag) ~= 'table' or type(model.GetMaxItemNum) ~= 'function' then return panel end
    -- The native replacement prompt displays the first item past capacity.
    local capacity = model.GetMaxItemNum()
    local id = reward_choice_id(bag[index])
    if type(capacity) ~= 'number' or index ~= capacity + 1 or not id then return panel end
    -- The native reply removes one overflow item before the next presentation.
    -- Use the remaining count to distinguish identical incoming IDs in one batch.
    local identity = tracking.replacement
    if not identity or identity.view ~= view.self or identity.bag ~= bag
        or identity.count ~= #bag or identity.index ~= index or identity.id ~= id then
        identity = {view=view.self, bag=bag, count=#bag, index=index, id=id}
        tracking.replacement = identity
    end
    panel.identity = identity
    panel.subject_id = id
    panel.rows = {{id=id, index=1, disabled=false, selected=false}}
    panel.discard = buttons.RogueItemReplace_Btn_Del_Click
    panel.ready = type(panel.discard) == 'function'
    return panel
end

local function apply_reward_choice(panel, command, row)
    if panel.kind == 'replacement' and command == 'discard' and panel.ready then
        panel.discard(nil, nil)
        return 'submitted'
    end
    if panel.kind ~= 'cards' or not panel.ready then return 'waiting' end
    for _, current in ipairs(panel.rows) do
        if current.selected and current.id ~= row.id then return 'waiting' end
    end
    if command == 'select' then
        if row.selected then return 'applied' end
        panel.controller.Select(row.id)
        local current = read_reward_choice(UIManager:GetCurrentPanelURL())
        local selected = current and current.rows[row.index]
        return current and current.identity == panel.identity and selected
            and selected.id == row.id and selected.selected and 'applied' or 'waiting'
    elseif command == 'confirm' and row.selected and panel.can_confirm then
        panel.confirm(nil, nil)
        return 'submitted'
    end
    return 'waiting'
end
)lua";

} // namespace bridge
