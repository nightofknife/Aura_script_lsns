# Resonance input and capture bridge

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
stages the packaged bootstrap and Unity profiles in the per-user cache and
starts it with the adjacent loader. The bootstrap loads a staged
`resonance_bridge_host.dll` whose SHA-256 matches the requested build.
The process must match a fingerprint profile. Bridge tasks use Unity's
1280×720 render viewport for both input and capture, including while minimized.
Frames are read at the end of Unity's render loop and published to a local
shared-memory buffer at up to 30 FPS. The same stream continues across window
minimize and restore. System mode uses the configured WGC capture path.

Switch modes only with no held buttons/keys or running gestures. Bridge mode's
`app.focus()` and `focus_with_input()` check logical input readiness. Restart
the game once when migrating from the original single-DLL bridge.

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

The bootstrap owns the Unity hook entry points and the pipe server. The host
owns a frame snapshot and executes Unity operations on the main thread.
Pipe replies are bounded, session/sequence checked and pinned to the process
creation identity and host instance. Unknown outcomes are never replayed. Client process death
cancels owned input and restores background/focus behavior. Once a root task
and its readers finish, Aura stops frame production and closes the session and
the shared buffer. The host unloads after 30 seconds without a session. A
different requested build or a shutdown request can unload an idle host sooner.
The bootstrap stops admitting calls and waits for every admitted call to return
before releasing the host module. The next bridge task loads the requested
build automatically. The bootstrap remains mapped for the game's lifetime.

The cache defaults to `%LOCALAPPDATA%/AuraResonance/input_bridge`.
`AURA_INPUT_BRIDGE_CACHE` can set the cache for the next bootstrap load.
`status` reports `host_state`, `host_hash`, `host_instance` and
`lifecycle_error`. Future incompatible bootstrap changes require a game restart.

Current live qualification and limitations are in `../docs/input-bridge-native-results.md`.
