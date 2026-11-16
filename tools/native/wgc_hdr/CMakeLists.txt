cmake_minimum_required(VERSION 3.23)
project(aura_wgc_hdr LANGUAGES CXX)
add_library(aura_wgc_hdr SHARED capture.cpp display.cpp)
target_compile_features(aura_wgc_hdr PRIVATE cxx_std_20)
target_compile_definitions(aura_wgc_hdr PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
target_link_libraries(aura_wgc_hdr PRIVATE d3d11 dxgi windowsapp runtimeobject CoreMessaging)
# CPU and GPU releases build the same Plan asset independently.
target_link_options(aura_wgc_hdr PRIVATE /Brepro)
set_property(TARGET aura_wgc_hdr PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
