# Project Working Rules

## Test Execution

- Run every test, validation, smoke check, and packaging check from the repository root.
- Keep all temporary files, caches, screenshots, logs, and generated test artifacts inside this repository.
- Pytest temporary directories must stay under `.pytest_tmp`. The repository `pytest.ini` uses that directory by default; scoped runs may use `.pytest_tmp/<scope>`, but must not point `--basetemp` outside the repository.
- For non-pytest tools that use the operating-system temporary directory, set `TEMP`, `TMP`, and `TMPDIR` to a dedicated directory under `.pytest_tmp/<scope>` before running them.
- Do not use the Windows system temporary directory, Desktop, another checkout, or any external project directory for testing unless the user explicitly requests that target.
