# Run of show

All times are Eastern, Tuesday, November 3, 2026.

## Before class

Keep the Mac on power with the lid open until you leave for class. The pipeline
runs under `caffeinate`, which prevents idle sleep. Closing the lid still
sleeps the machine and pauses collection; see "Moving to the classroom".

| When | Do |
|---|---|
| 12:00 pm | `uv run monitor ref build-races`, then `uv run monitor sources check`. Review `/status`. |
| 12:30 pm | Press `r` for replay, set 20×, and watch the last 6 hours. Check every panel at 1280×720 and 1920×1080. |
| 2:30 pm | `scripts/restart_pipeline.sh`, restart the dashboard, and run `uv run monitor backup --now`. |
| 2:30 pm | Confirm spend and caps with `uv run monitor cost`. Connect the projector and press `p`. |
| 3:00 pm | `git tag known-good` on the running commit. Rollback goes to this tag. |

## Starting the system

```bash
scripts/restart_pipeline.sh              # the collector, detached
uv run monitor web --reload              # the dashboard, reloads on code changes
```

The dashboard is at <http://127.0.0.1:8000>. Add `--lan` to reach it from a
second device on the same network.

## Moving to the classroom

Closing the lid pauses the pipeline and the dashboard. Nothing needs stopping
or restarting, and nothing already collected is at risk.

1. Close the lid and travel.
2. Open the lid, plug in, and join the network. Finish any Wi-Fi login page in
   a browser.
3. Watch the strip. It reads "Offline since" until the network is confirmed,
   then names the gap for 30 minutes.

When the network returns, every feed polls within 30 seconds. Stories published
during the gap are placed at their published time, and time charts mark the gap
with "Offline" and "Back online". `/status` lists every gap.

A short network drop behaves the same way, with no action needed. Polling
waits, no feed is penalized, and articles and model calls wait instead of being
skipped or falling back to keywords.

A feed lists only its newest stories. After a gap of 5 minutes or more, the
news sitemaps of the outlets in `config/news_sitemaps.csv` are read once to
recover stories that left a feed. These stories have a title and date but no
feed summary. A story that left a feed at any other outlet is not recovered.

With no network at all, the dashboard still runs from local data, including
replay. A phone hotspot works; collection reads about 650 MB per hour before
HTTP compression.

## During class

Watch the status strip. It shows pipeline lag, LLM state, spend today, and free
disk.

If lag stays above 3 minutes, set feeds to a lower tier in `config/sources.csv`
or disable tier 3 feeds and restart the pipeline. If that is not enough, stop
Stage B for international sources by disabling those sources.

## Changing things live

Collection and the dashboard are separate processes. Restarting the dashboard
never interrupts collection.

| Change | What it takes | Cost |
|---|---|---|
| Template, CSS, or JavaScript | Save the file and reload the page. Recompile CSS with `bin/tailwindcss -i src/monitor/web/static/css/input.css -o src/monitor/web/static/css/app.css` | None |
| Panel query or web code | Save the file. `--reload` restarts the dashboard, and browsers reconnect | None |
| A dashboard or poller setting in `.env` | Restart that process | None |
| Sources, watchlist, or race files in `config/` | Saved files load within a minute. New feeds need a pipeline restart | None |
| Parser code | `scripts/restart_pipeline.sh`. It takes about 10 seconds and resumes where it stopped. Feeds polled in that gap are picked up on the next poll | None |
| A prompt or a parser's logic, reprocessing old items | Bump the parser's `version` and restart. Old items reprocess in the background behind live items | API spend per item; see `monitor cost` |
| A new table | Add it to `TABLES` in `src/monitor/lake.py` and restart the pipeline | None |
| Changing an existing table's columns | Avoid this during class. Add a new table instead | None |

Use a scratch lake to try anything risky: set `PATHS__CATALOG` and
`PATHS__DATA` for that one command. Only the pipeline writes to the real lake.

To roll back:

```bash
git switch --detach known-good
scripts/restart_pipeline.sh
```

Restart the dashboard the same way if it is not running with `--reload`. The
data stays, because tables are append-only and rows written by newer code
remain readable.

## If something goes wrong

| Symptom | What happens | What to do |
|---|---|---|
| The network drops, or a Wi-Fi login page appears | Polling waits and no feed is penalized. The strip says "Offline since". Every feed polls within 30 seconds of the network's return | Finish any login page. Otherwise nothing |
| LLM errors while the network is up | After 2 minutes, classification uses keywords and extraction waits. The strip says "LLM degraded" | Nothing. It recovers by itself and catches up |
| Lag over 2 minutes | The pipeline slows embedding and pauses backfills until lag falls under 1 minute | Check `/status` for the slow stage |
| Free disk under 10 GB | A yellow banner | `uv run monitor prune --dry-run --thin 15m --older-than 2h`, then run it without `--dry-run` |
| Free disk under 5 GB | A red banner, and full-text fetching stops | Prune as above. Offload if a bucket is configured: `uv run monitor offload` |
| Free disk under 3 GB | The pipeline thins snapshots by itself | Free space elsewhere on the disk |
| A feed keeps failing | It retries at most 15 minutes apart. `/status` shows it | Nothing |
| The dashboard feels slow | When uncached queries pass `WEB__SLOW_QUERY_MS` at p95, panels refresh at most every 20 seconds until p95 falls below half of it | Projector mode shows fewer panels |
| The dashboard is down after an edit | Collection continues. A file with an error stops the dashboard until the file is saved again | Fix the file, or `git checkout` it |
| The dashboard is down | Collection continues | `uv run monitor web --reload` |
| The pipeline is down | Polling stops, and the strip reports it | `scripts/restart_pipeline.sh` |
| The catalog is damaged | Queries fail | Stop the pipeline, then restore the newest backup, as described below |

### Restoring the catalog

```bash
kill -TERM "$(cat data/writer.lock)"
mv data/catalog.sqlite data/catalog.sqlite.damaged
zstd -d "$(ls -t data/backups/catalog-*.zst | head -1)" -o data/catalog.sqlite
scripts/restart_pipeline.sh
```

Items committed after that backup, at most 15 minutes' worth, are fetched again
only if the feeds still list them.

## After class

Reduce polling by raising `POLL__TIER_SECONDS`, for example to
`[180,600,1800]`, and restart the pipeline. Review disk use with `uv run
monitor disk` and `uv run monitor prune --dry-run`. If a bucket is configured,
`uv run monitor offload --lake` uploads the current Parquet files.
