# Resonance mouse input bridge

This plan-owned Windows x64 bridge preserves the existing `app`/`controller` API.
It uses a native host and local named pipes; Frida is not a runtime dependency.

## Enable

The default remains system input. In `plans/resonance_pc/config.yaml` set:

```yaml
resonance_pc:
  input:
    mode: bridge
    stop_inertia: true
```

Start the game normally, then run an existing task. The first bridge operation
loads `assets/input_bridge/resonance_bridge_host.dll` using the adjacent loader.
The process must match a fingerprint profile and have a visible, non-minimized
client area. The qualified profile currently requires a 1:1 client/render size.
WGC remains the framework screenshot backend.

Switch modes only with no held buttons/keys or running gestures. The bridge never
falls back to SendInput. Explicit `app.focus()` still focuses the OS window;
`focus_with_input()` in bridge mode only acquires logical input readiness.

## Operations

- `click`, `move_to`, `move_relative`, `mouse_down`, `mouse_up`, `scroll`.
- `app.drag` and `controller.drag_to`, including async forms, are complete
  gestures. Targets are resolved from raycasts, handlers and viewport geometry;
  no scene names or fixed object paths are used.
- `stop_inertia=False` on a complete drag preserves ScrollRect inertia. Default
  `None` follows plan configuration. Existing endpoint hold time is still honored.
- `cancel_input()` cancels and cleans up a gesture; `release_all()` releases held
  input. These are different operations.
- Keyboard, text and relative look commands are deliberately unsupported in v1.

See `../docs/input-bridge-contract.md` for defaults and exact parameter semantics.
Long physical-style holds can invoke the game's own long-press handling; they are
not guaranteed to become a short click when released.

## Build

Use PowerShell 7 from the repository root with CMake, Ninja and an x64 C++17
Windows compiler. LLVM-MinGW is supported:

```powershell
./plans/resonance_pc/bridge/tools/build.ps1 `
  -CMake 'C:/tools/cmake/bin/cmake.exe' `
  -Ninja 'C:/tools/ninja.exe' `
  -ToolchainBin 'C:/tools/llvm-mingw/bin'
```

For MSVC, run from its configured development environment and omit ToolchainBin.
Build and temporary output stay under repository `.pytest_tmp`. Installed runtime
files and third-party licenses go to `assets/input_bridge`, which the existing
release-plan assembler includes. CMake fetches pinned MinHook and nlohmann/json
versions during development; the installed runtime needs no network download.

## Version and lifecycle

Profiles match SHA256 of GameAssembly, UnityPlayer and IL2CPP metadata. Unsupported
updates fail closed. Capability flags represent qualified native behavior, not
merely successful DLL loading; changing hashes alone is not an update procedure.

The host owns a frame snapshot and executes Unity operations on the main thread.
Pipe replies are bounded, session/sequence checked and pinned to the process
creation identity. Unknown outcomes are never replayed. Client process death
cancels owned input and restores background/focus behavior. Closing a session
leaves the native module inert and reusable until the game exits; it does not
force-unload code still referenced by Unity.

Current live qualification and limitations are in `../docs/input-bridge-native-results.md`.
