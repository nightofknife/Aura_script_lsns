# Project Working Rules

## Release Naming

- Application version strings, Git release tags, and GitHub Release titles must use only `vX.X.X`, for example `v1.11.8`.
- A GitHub Release title must exactly match its release tag. Do not append Chinese text, feature summaries, product names, platform names, or CPU/GPU labels.
- Put feature summaries and other descriptive text in the release notes. Keep downloadable artifact filenames aligned with the existing release contract.

## Test Execution

- Run every test, validation, smoke check, and packaging check from the repository root.
- Keep all temporary files, caches, screenshots, logs, and generated test artifacts inside this repository.
- Pytest temporary directories must stay under `.pytest_tmp`. The repository `pytest.ini` uses that directory by default; scoped runs may use `.pytest_tmp/<scope>`, but must not point `--basetemp` outside the repository.
- For non-pytest tools that use the operating-system temporary directory, set `TEMP`, `TMP`, and `TMPDIR` to a dedicated directory under `.pytest_tmp/<scope>` before running them.
- Do not use the Windows system temporary directory, Desktop, another checkout, or any external project directory for testing unless the user explicitly requests that target.
