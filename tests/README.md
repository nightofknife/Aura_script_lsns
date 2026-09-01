# Test layers

The repository separates tests by the kind of guarantee they provide:

- `contracts/`: static architecture and release-policy invariants. Update these
  deliberately when the corresponding design decision changes.
- `unit/`: deterministic business, GUI-state, and algorithm behavior. Prefer
  public inputs and observable outputs over private helper names or source text.
- `smoke/`: minimal startup, discovery, runner, GUI self-check, and filesystem
  compatibility checks. These answer whether the source runtime can operate.

Packaged executable smoke checks remain in `scripts/release/validate_release.py`
and run after PyInstaller assembly. A release is allowed only when all four
layers pass, but GitHub Actions reports contracts, unit behavior, and source
smoke separately so a stale expectation is distinguishable from a runtime
failure.

Test doubles for framework services should implement the public interface they
stand in for. Avoid assertions on raw workflow source or private implementation
details unless that exact structure is the documented contract.
