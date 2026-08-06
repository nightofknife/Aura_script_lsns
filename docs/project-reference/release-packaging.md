# Release Packaging

`scripts/package_release.ps1` is the only supported release entrypoint for both
local builds and GitHub Actions. It creates profile-specific Python 3.12
environments, installs hashed dependency locks, verifies immutable runtime
assets, delegates directory assembly to the internal builder, runs smoke tests,
and creates validated archives.

## Outputs

```text
AuraResonance-<label>-win-x64-cpu.zip
AuraResonance-<label>-win-x64-gpu.zip
AuraResonance-<label>-nvidia-cu13-overlay.zip
SHA256SUMS.txt
```

Both full packages contain `AuraResonanceGui.exe`, the frozen runtime, OCR
models, default config, and the complete filtered external `plans/` source tree.
Plan caches, state, logs, screenshots, credentials, and bytecode are excluded.
Standalone Plan replacement packages are not produced.

## Canonical commands

```powershell
pwsh .\scripts\package_release.ps1 -Profile cpu -ReleaseLabel local
pwsh .\scripts\package_release.ps1 -Profile gpu -ReleaseLabel local
pwsh .\scripts\package_release.ps1 -Profile overlay -ReleaseLabel local
pwsh .\scripts\package_release.ps1 -Profile all -ReleaseLabel local
```

The `all` profile is the preferred local release rehearsal. It validates the
CPU, GPU, and overlay as one set and writes `SHA256SUMS.txt` only after every
check passes. A dirty source tree fails by default; `-AllowDirty` is reserved for
local test packages and is recorded in `BUILD-INFO.json`. Use `-Offline` to
forbid asset downloads.

## Sources of truth

- `packaging/release-contract.json` defines profiles, names, providers, required
  files, permitted CPU/GPU differences, overlay DLLs, and archive limits.
- `requirements/release-*.lock.txt` contain complete Windows/Python 3.12 locks
  with wheel hashes. Regenerate them with `scripts/update_release_locks.ps1`.
- `packaging/assets/mumu-runtime.lock.json` pins MuMu helper downloads by URL,
  size, and SHA256. OCR remains pinned to the model Release and checksum named in
  the release contract.

`scripts/build_release.ps1` is internal. It only builds and assembles directory
trees; direct callers do not receive dependency preparation, smoke tests,
archives, or consistency validation.

## Validation

Every CPU and GPU build runs CLI discovery, task discovery, CPU OCR doctor, GUI
self-check, excluded-payload scanning, execution-provider checks, and archive
safety checks. The overlay is restricted to its notice and
`runtime/_internal/nvidia` payload.

The release-set validator then requires matching commit, label, contract, model
and MuMu fingerprints; identical Plan/model trees; matching common dependencies;
only allowlisted CPU/GPU differences; and a collision-free GPU/overlay merge.
