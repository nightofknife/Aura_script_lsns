#pragma once

#include <nlohmann/json.hpp>

namespace bridge {
bool deep_dive_board_available();
nlohmann::json read_deep_dive_board(bool turn_only, bool controls_only = false);
} // namespace bridge
