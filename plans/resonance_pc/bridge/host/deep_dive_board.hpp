#pragma once

#include <nlohmann/json.hpp>

namespace bridge {
bool deep_dive_board_available();
nlohmann::json read_deep_dive_board(bool turn_only, bool controls_only = false);
nlohmann::json execute_deep_dive_action(const nlohmann::json &args);
nlohmann::json execute_deep_dive_choice(const nlohmann::json &args);
} // namespace bridge
