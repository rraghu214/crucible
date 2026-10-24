# Hooks

Hooks are Python files that Crucible runs around each experiment (DESIGN.md §10):
database reset and reseed, token refresh, log capture. Decided by the operator on
24 October 2026: **Python only.** There is never a question of whether `psql`,
`curl` or a shell exists on the box. The hook uses Crucible's own Python.

## The three stages

| Stage | When | If it fails |
|---|---|---|
| `before_each` | before each experiment is diagnosed | the experiment is **blocked** and the campaign aborts |
| `after_each` | after each experiment's verdict | the campaign aborts before the next one |
| `on_abort` | when a campaign aborts | recorded; nothing further to block |

A reset that silently did nothing would make every later measurement wrong.
That is why a failing hook blocks rather than warns.

## How to add one

1. Write a `.py` file. Exit `0` on success and anything else on failure. Output
   (the first 2 KB) is recorded on the manifest.
2. **New Test Run → Scenarios → Hooks → Upload hook (.py).** It needs the control
   token, accepts at most 200 KB, and is syntax-checked but **not run** on
   upload. Files are stored in `$CRUCIBLE_STATE_DIR/hooks/` (default
   `~/.crucible/hooks/`).
3. Pick the file for a stage, set the timeout, and list any environment
   variables the hook needs.
4. **Save changes** or **Launch** writes the `hooks:` line in `config/slo.yaml`:
   ```yaml
   hooks: {"before_each": "reset_db.py", "timeout_s": 120, "pass_env": ["PGPASSWORD", "PGHOST"]}
   ```
   You can also edit that line by hand. A running campaign picks up an edit at
   its next experiment boundary (Group 33).

## What a hook runs as, and what it can see

- **Command:** `python -I <file>`: Crucible's interpreter in isolated mode. It
  ignores `PYTHONPATH`, `PYTHON*` variables and the user site directory, and no
  shell is involved.
- **Working directory:** the target's workspace (the `--workspace` checkout).
- **Environment: an allowlist, never the full environment.**
  - basic OS variables: `PATH`, `TEMP`/`TMP`/`TMPDIR`, `HOME`/`USERPROFILE`,
    `LANG`/`LC_*`, `TZ`, `SYSTEMROOT`/`WINDIR`, `PYTHONIOENCODING`;
  - the names you list in `pass_env`, for example a database password;
  - `CRUCIBLE_HOOK_STAGE`, `CRUCIBLE_HOOK_RUN_ID` and
    `CRUCIBLE_HOOK_EXPERIMENT` (empty for `on_abort`).

  **Crucible's own variables are never passed**: not the control token, not the
  gateway keys, not Datadog keys. A `pass_env` entry starting `CRUCIBLE_` is
  refused.
- **Timeout:** 1–3600 s (default 60). A hook that overruns is killed and counts
  as failed.

### Example: reset a table before each experiment

```python
"""reset_db.py: truncate and reseed orders so every experiment starts equal."""
import os
import sys

import psycopg  # must be installed in Crucible's environment: uv add psycopg

with psycopg.connect(host=os.environ["PGHOST"], password=os.environ["PGPASSWORD"],
                     user="perflab", dbname="perflab") as conn:
    conn.execute("TRUNCATE orders; INSERT INTO orders SELECT * FROM orders_seed;")
print("orders reset for experiment", os.environ.get("CRUCIBLE_HOOK_EXPERIMENT"))
sys.exit(0)
```

Hooks run on **Box B** (where Crucible runs), so a database reset reaches
Postgres on Box A over the network. Any library a hook imports must be installed
in Crucible's Python environment.

## Recommended: run hooks as a low-privilege user (setup step)

Python cannot be fenced off from inside Python, so the real wall around a hook
is the operating system. On Box B, give hooks their own user with **no read
access** to Crucible's code, state or reports:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin crucible-hooks
chmod 750 ~/crucible ~/.crucible              # crucible-hooks cannot read these
sudo mkdir -p /srv/crucible-hooks && sudo chown "$USER":crucible-hooks /srv/crucible-hooks
sudo chmod 750 /srv/crucible-hooks
```

Then run the hook as that user (for example with
`sudo -u crucible-hooks python3 -I <file>` and a matching sudoers rule). Wiring
that into Crucible is not built yet; the environment allowlist above is what is
enforced today. This step is listed in the product setup instructions.
