# Protected service acceptance

These tests are operator-owned and must not be edited by the runtime builder. `manifest.json` maps each scenario to every mandatory pytest node ID. The validation runner must collect and pass all selected IDs with no skips; absence of failed tests alone is insufficient.

`greenfield` requires the core suite, `brownfield` additionally requires expiration and additive-migration checks, and `ambiguous` additionally requires aliases. Set `SCENARIO` accordingly. Alias checks read the approved `ALIAS_MIN_LENGTH` and `ALIAS_MAX_LENGTH` bounds, defaulting to 4 and 32. Import the candidate's `shortener.app.create_app` through `PYTHONPATH`. Fail if the selected candidate cannot be imported.

Helpers create fresh UUID directories beneath `TEST_DATA_ROOT`, defaulting to `/tmp/shortener-acceptance`, and retain them. Tests do not delete files or use automatic temporary-directory cleanup. Disable pytest's cache provider and Python bytecode writes in the runner. The ordinary test module imports need no `conftest.py`.

The suite includes in-process ASGI HTTP integration, input validation, collision exhaustion and recovery, concurrent count updates, persistent state, database failure responses, expiration boundaries, preservation of prior schema data, and alias validation. It does not claim production load, Internet exposure, or actual target navigation.
