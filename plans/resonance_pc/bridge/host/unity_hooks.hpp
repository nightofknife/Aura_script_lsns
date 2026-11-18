#pragma once

#include <string>

namespace bridge {
void start_unity_hooks();
bool unity_hooks_ready();
bool unity_capture_available();
std::string unity_hook_error();
} // namespace bridge
