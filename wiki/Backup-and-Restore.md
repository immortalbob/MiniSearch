# Backup & Restore

Mnemolis's entire persisted state lives in exactly seven files, all under `/app/data` inside the container:

| File | What it holds |
|------|-----------------|
| `cache.json` | The [result cache](Caching#result-cache) |
| `routing_cache.json` | The [routing cache](Caching#routing-cache) |
| `query_log.db` | The query log — every request's timestamp, source, success, latency, and fallback flag |
| `snapshots.db` | [Snapshot history](Snapshot-Engine-and-Changes) for the background diff engine |
| `adversarial_testing.db` | [Adversarial self-testing](Adversarial-Self-Testing) combination history — synthetic generated queries only, never real user queries |
| `temporal_patterns.db` | [Cross-source temporal pattern detection](Cross-Source-Temporal-Pattern-Detection) event history and mined pattern candidates |
| `history.db` | [History source](History-and-Trends) metric samples and catalog — only present once `HISTORY_ENABLED` has been turned on; backing it up is what preserves the recorded time series across a rebuild, since the feature cannot retroactively re-record the past |

`GET /backup` tars all seven into a downloadable `.tar.gz` (files that don't exist yet — like `history.db` before the feature's first enablement — are simply skipped). `GET /backup/info` shows what's currently in each file (size, last-modified time) without actually creating a backup — useful for a quick "is this worth backing up right now" check.

**The SQLite files are snapshotted through SQLite's own online backup API before tarring (v3.52.0).** Every Mnemolis database runs in WAL mode, which means the main `.db` file on disk is *not* the complete database at an arbitrary moment — recently committed writes live in the `-wal` sidecar file until the next checkpoint. A function-by-function audit found the previous behavior (tar the bare `.db` directly) silently produced backups missing every not-yet-checkpointed write, and a copy taken mid-commit could even be internally inconsistent. `Connection.backup()` exists for exactly this: it produces a complete, consistent, checkpointed single-file snapshot while concurrent writers keep working, no downtime needed. Each `.db` is snapshotted to a temp file first and the snapshot is what enters the tarball; the JSON files (already written atomically) are tarred directly as before. If the backup API fails on a given file — a genuinely corrupted database — the endpoint falls back to the old raw copy with a logged warning, since a possibly-stale backup of that one file still beats silently omitting it. The regression test for this constructs the exact hazard (WAL mode, autocheckpointing off, the writer connection held open across the call) and asserts the un-checkpointed row survives extraction — confirmed to fail against the old behavior.

## What's deliberately *not* in the backup

Kiwix ZIM files, your `docker-compose.yml`, and `searxng/settings.yml` aren't included. `/backup` only covers Mnemolis's own internal state — back up your ZIM files and compose configuration separately, as part of your normal homelab backup routine. None of these would be useful to restore without the rest of your actual deployment configuration intact anyway.

## A real gotcha worth knowing before you need it: volume naming

Docker Compose automatically prefixes named volumes with your **project name**, which defaults to whatever folder `docker-compose.yml` lives in. A volume declared as `mnemolis_data` in your YAML doesn't actually get created with that exact name — if your folder is `minisearch/`, the real volume Docker creates is `minisearch_mnemolis_data`.

This matters specifically the moment you try to restore manually with `docker run -v`, because that command needs the *real* volume name, not the name written in the YAML. Always check first:

```bash
docker volume ls | grep data
# or, for a running container, the most direct check:
docker inspect mnemolis --format '{{json .Mounts}}' | python3 -m json.tool
```

`docker inspect` against the actual running container is the more reliable of the two — it shows you exactly which host path or named volume is genuinely mounted where, right now, rather than asking you to infer it from a list of volume names that might not obviously correspond to the right container.

If you want a stable, predictable volume prefix regardless of what the folder happens to be named, set it explicitly:

```bash
echo "COMPOSE_PROJECT_NAME=mnemolis" > .env
```

## Restoring

```bash
docker compose down

docker run --rm -v mnemolis_data:/app/data -v $(pwd):/backup alpine \
  sh -c "cd /app/data && tar xzf /backup/mnemolis-backup.tar.gz"

docker compose up -d
```

Replace `mnemolis_data` in that command with whatever `docker volume ls` actually reported — not the bare name from `docker-compose.yml` — if you haven't set `COMPOSE_PROJECT_NAME`.

## Why this volume-naming detail earned its own section

It's the kind of thing that looks like a Mnemolis bug the first time it bites someone — a restore command that silently does nothing, or appears to succeed while writing into a volume nothing's actually mounting — when the real cause is just Docker Compose's own naming convention being non-obvious. Worth checking the real volume name *before* assuming anything's broken, the same instinct that mattered when diagnosing [The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson) — verify what's actually true on disk and in the running container before trusting what a config file or command *should* have done.
