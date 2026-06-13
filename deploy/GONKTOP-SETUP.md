# Gonktop Deployment Runbook

Target machine: **gonktop** (the always-on Linux/macOS box at 192.168.0.58).  
All commands run as the service user (e.g. `herren`) unless noted.

---

## 1. Clone the repo and create the virtual environment

```bash
cd ~/dev
git clone <repo-url> palimpsest
cd palimpsest
uv sync
```

`uv sync` reads `pyproject.toml` and populates `.venv/`. All subsequent commands
that reference the interpreter use `.venv/bin/python`.

---

## 2. Install the spaCy language model

The model is not a PyPI package; install it as a wheel via the spaCy download shorthand:

```bash
cd ~/dev/palimpsest
uv run python -m spacy download en_core_web_sm
```

Verify it loads:

```bash
uv run python -c "import spacy; spacy.load('en_core_web_sm'); print('ok')"
```

---

## 3. Create / verify config.toml

The repository ships a working `config.toml` with `storage.root` pointing at the
developer machine path.  **On gonktop you must update the storage root** to the
external drive:

```bash
cp config.toml config.toml.bak   # keep a backup
```

Edit `config.toml` and set:

```toml
[storage]
root = "/Volumes/palimpsest"
```

All other derived paths (e.g. `db.path`) use `{storage.root}` as a template and
will resolve automatically.

Key sections to confirm are present and correct:

| Section | Field | Expected (gonktop) |
|---------|-------|--------------------|
| `[storage]` | `root` | `/Volumes/palimpsest` |
| `[broker]` | `port` | `8077` |
| `[mcp]` | `port` | `8078` |
| `[nodes]` | `gonktop` | `["ocr", "features", "embed", "gapjoin"]` |

Validate that the config loads without errors:

```bash
uv run python -c "from palimpsest.config import load; load(); print('config ok')"
```

---

## 4. Run the database migration

This creates (or upgrades) the SQLite database at `{storage.root}/db/palimpsest.db`
and brings it to schema version 2:

```bash
cd ~/dev/palimpsest
uv run python -m palimpsest.db migrate
```

Expected output:

```
Migrating database…
Done. Schema version: 2
```

Re-running is safe — the migration is idempotent.

---

## 5. Install launchd services

The three `deploy/` plists cover the **broker**, **MCP server**, and **worker**.
Copy them to the per-user LaunchAgents directory and load them:

```bash
# Create the directory if it doesn't exist
mkdir -p ~/Library/LaunchAgents

# Copy all three plists
cp ~/dev/palimpsest/deploy/com.palimpsest.broker.plist   ~/Library/LaunchAgents/
cp ~/dev/palimpsest/deploy/com.palimpsest.server.plist   ~/Library/LaunchAgents/
cp ~/dev/palimpsest/deploy/com.palimpsest.worker.plist   ~/Library/LaunchAgents/

# Load and start each service
launchctl load -w ~/Library/LaunchAgents/com.palimpsest.broker.plist
launchctl load -w ~/Library/LaunchAgents/com.palimpsest.server.plist
launchctl load -w ~/Library/LaunchAgents/com.palimpsest.worker.plist
```

Give the processes a few seconds to start, then check status:

```bash
launchctl list | grep palimpsest
```

A PID in the second column means the service is running.  A `-` with exit code `0`
means it exited cleanly (unexpected); a non-zero exit code means it crashed — check
the log files (step 7).

### Reloading after plist changes

```bash
launchctl unload ~/Library/LaunchAgents/com.palimpsest.broker.plist
launchctl load   ~/Library/LaunchAgents/com.palimpsest.broker.plist
```

---

## 6. Run the preflight check

```bash
cd ~/dev/palimpsest
uv run python -m palimpsest.preflight
```

All 8 checks must show `PASS`:

```
  PASS  Config loads (config.toml)
  PASS  Storage root mounted and writable (/Volumes/palimpsest)
  PASS  Storage free space ≥ 200 GB
  PASS  DB migrated (schema_version = 2)
  PASS  Broker reachable (localhost:8077)
  PASS  Worker heartbeat present
  PASS  Ollama embed model warm latency < 3 s
  PASS  spaCy en_core_web_sm loads
```

Exit code 0 = all green.  Exit code 1 = at least one failure — address each `FAIL`
line before proceeding.

---

## 7. Log files

| Service | stdout / stderr |
|---------|-----------------|
| Broker  | `/tmp/palimpsest-broker.log` |
| MCP server | `/tmp/palimpsest-server.log` |
| Worker  | `/Users/herren/palimpsest-worker.out.log` (stdout) / `…err.log` (stderr) |

Tail a log:

```bash
tail -f /tmp/palimpsest-broker.log
```

---

## 8. Stopping services

```bash
launchctl unload ~/Library/LaunchAgents/com.palimpsest.broker.plist
launchctl unload ~/Library/LaunchAgents/com.palimpsest.server.plist
launchctl unload ~/Library/LaunchAgents/com.palimpsest.worker.plist
```

The worker handles `SIGTERM` gracefully: it finishes the current job before exiting.

---

## 9. Troubleshooting

| Symptom | First check |
|---------|-------------|
| Broker won't start | `cat /tmp/palimpsest-broker.log` — missing venv or config.toml? |
| Preflight fails "Storage root" | Is `/Volumes/palimpsest` mounted? `diskutil list` |
| Preflight fails "Broker reachable" | `launchctl list \| grep broker` — is the service running? |
| Preflight fails "spaCy" | Re-run step 2 to install `en_core_web_sm` |
| Preflight fails "Ollama" | `curl http://localhost:11434/api/tags` — is Ollama running? |
| DB errors | Delete the DB file and re-run `python -m palimpsest.db migrate` |
