# Release Packaging

`scripts/build_release.ps1` builds a Windows release with external editable plans.

Important output names:

- `AuraResonanceGui.exe`
- `runtime\AuraResonanceRuntime.exe`
- `runtime\aura.exe`

The release root copies all manifest-backed plan packages from `plans/`, including `aura_base`, `aura_benchmark` and `resonance`. The release does not include old game-specific assets that are no longer present in this repository.

Example build:

```powershell
.\scripts\build_release.ps1 -IncludeGui -CreateZip -ReleaseName aura-resonance-release
```

Example release smoke:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <release>\run.ps1 tasks resonance
& <release>\runtime\AuraResonanceRuntime.exe --self-check
```
