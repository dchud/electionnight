# Run of show

All times are Eastern, Tuesday, November 3, 2026.

## Before class

Keep the Mac on power with the lid open until you leave for class. The pipeline
runs under `caffeinate`, which prevents idle sleep. Closing the lid still
sleeps the machine and pauses collection; see "Moving to the classroom".

Before you leave:

1. Run `uv run monitor ref build-races --dry-run`. Compare the battleground
   states it prints with `battleground_states` in `config/races.yaml`. If they
   differ and you accept the new list, run the command without `--dry-run`. The
   file loads within 2 minutes, with no restart.
2. Run `git tag known-good` on the running commit. Rollback goes to this tag.

## Starting the system

```bash
scripts/restart_pipeline.sh              # the collector, detached
uv run monitor web --reload              # the dashboard, reloads on code changes
```

The dashboard is at <http://127.0.0.1:8000>. Add `--lan` to reach it from a
second device on the same network. Other devices can read every page but cannot
change anything; their watchlist buttons have no effect.

## Moving to the classroom

The trip falls between 5:30 and 7:30 pm, with the laptop asleep in a closed bag
and no connection. Polls close at 6:00 in IN and KY, at 7:00 in FL, GA, NH, SC,
VA, and VT, and at 7:30 in NC, OH, and WV. The 8:00 and 9:00 closings come
after arrival.

Closing the lid pauses the pipeline and the dashboard. Nothing needs stopping
or restarting, and nothing already collected is at risk.

1. Unplug, close the lid, and travel.
2. Open the lid, plug in, and join the network. Finish any Wi-Fi login page in
   a browser.
3. Watch the strip. It reads "Offline since" until the network is confirmed,
   then names the gap for 30 minutes. Until the first commit after waking,
   within about 90 seconds, its Lag counts from before the trip and it adds
   "Pipeline is not committing".
4. Connect the projector and press `p`. This hides the strip; see "During
   class".

When the network returns, every feed polls within about a minute. Stories
published during the gap are placed at their published time, and time charts
mark the gap with "Offline" and "Back online". `/status` lists every gap of 60
seconds or more.

A short network drop behaves the same way, with no action needed. Polling
waits, no feed is penalized, and articles and model calls wait instead of being
skipped or falling back to keywords. A gap under 60 seconds is not recorded, so
it has no mark on the charts and no row on `/status`.

A feed lists only its newest stories. After a gap of 5 minutes or more, the
news sitemaps of the outlets in `config/news_sitemaps.csv` are read to recover
stories that left a feed. These stories have a title and date but no feed
summary. A story that left a feed at any other outlet is not recovered.

This reading finishes only when every sitemap has answered. If the network
drops during it, it continues when the network returns, however short the drop.
A sitemap that fails 3 times with the network up is given up, and
`logs/pipeline.jsonl` has a `catch_up_gave_up` line for it. One read takes at
most 500 new stories from one sitemap file, and a `feed_entries_capped` line
gives the number cut.

With no network at all, the dashboard still runs from local data, including
replay. A phone hotspot works; collection reads about 650 MB per hour before
HTTP compression.

Aim to be connected by 7:45 pm. Feeds poll within about a minute of the network
returning, and the backlog should clear within about 15 minutes, ahead of the
8:00 closings.

## During class

Projector mode hides the status strip, with its warnings and disk banners. A
second window in this browser also opens in projector mode. To watch status
during class, keep `/status` open in a second window on the laptop's screen, or
press `p` to show the strip and press it again to hide it.

The strip shows Lag, LLM state, spend today, and free disk. `/status` shows the
last commit, whether the pipeline is shedding, each stage's queue and backlog,
free disk, collection gaps, feed health, and LLM state and spend. Only the
strip shows "Offline since" and the warnings about held rows, rejected rows,
and the archive writer.

The two lag figures are different numbers:

- **The strip's Lag** is the seconds since the last commit. It stays under
  about 90 seconds while commits succeed, however large the backlog. Over 3
  minutes, the strip adds "Pipeline is not committing": commits are failing or
  the pipeline has stopped. Follow "The strip says rows are held back" or "The
  pipeline is down" below. Reducing feeds does not help.
- **Backlog lag** is the "Oldest live item" column on `/status`, in seconds,
  not the "Lag" column beside it.

If backlog lag stays above 3 minutes, move feeds to a higher-numbered tier in
`config/sources.csv` or disable tier 3 feeds, and restart the pipeline. Tier 3
polls every 600 seconds and tier 1 every 60. If that is not enough, disable the
international sources and restart the pipeline. This stops all collection and
parsing for those outlets, because no setting limits extraction by source.

A browser keeps 6 connections per server, and each dashboard tab showing on
screen holds one for its event stream. A hidden tab closes its stream and
refreshes when it returns. Keep at most 5 dashboard windows showing at once;
beyond that, every request on every page waits.

## Changing things live

Started as above, collection and the dashboard are separate processes, so
restarting the dashboard does not interrupt collection. Under `honcho start
pipeline web`, as in the README, one process exiting stops the other.

| Change | What it takes | Cost |
|---|---|---|
| Template, CSS, or JavaScript | Save the file and reload the page. Recompile CSS with `bin/tailwindcss -i src/monitor/web/static/css/input.css -o src/monitor/web/static/css/app.css` | None |
| Panel query or web code | Save the file. `--reload` restarts the dashboard, and browsers reconnect | None |
| A dashboard or poller setting in `.env` | Restart that process | None |
| Sources, watchlist, or race files in `config/` | Saved files load within 2 minutes. A change to feeds, tiers, or `enabled` in `config/sources.csv` reaches polling and parsing only after a pipeline restart | None |
| Parser code | `scripts/restart_pipeline.sh`. It takes about 10 seconds and resumes where it stopped. Feeds polled in that gap are picked up on the next poll | None |
| A prompt or a parser's logic, reprocessing old items | Bump the parser's `version` and restart. Old items reprocess in the background behind live items. A later stage runs an item only if it still owes that item at its own version | API spend per item; see `uv run monitor cost` |
| A new table | Add it to `TABLES` in `src/monitor/lake.py` and restart the pipeline | None |
| Changing an existing table's columns | Avoid this during class. Add a new table instead | None |

Use a scratch lake to try anything risky. `SCRATCH_DIR` places every runtime
path under one directory, including the writer lock, the control directory, and
the backups, and gives that directory its own copy of `config/`. Set it for
each command:

```bash
SCRATCH_DIR=tmp/scratch uv run monitor init
SCRATCH_DIR=tmp/scratch uv run monitor prune --dry-run --all-but-latest
```

A scratch directory starts with no lake rows, model files, or reference files.
A scratch pipeline polls real feeds. Moving the catalog or data path without
the rest of the lake's paths is refused. Without `SCRATCH_DIR`, `monitor`
commands act on the real lake.

To roll back:

```bash
git switch --discard-changes --detach known-good && scripts/restart_pipeline.sh
```

The switch discards uncommitted edits to tracked files, so commit any you want
to keep first. If the switch fails, the pipeline does not restart. If the
dashboard is not running with `--reload`, stop it with Ctrl-C in its terminal
and start it again with `uv run monitor web`. The data stays, because tables
are append-only and rows written by newer code remain readable.

## If something goes wrong

Rows that name the strip need it showing. In projector mode, press `p` to show
it.

| Symptom | What happens | What to do |
|---|---|---|
| The network drops, or a Wi-Fi login page appears | Polling waits and no feed is penalized. The strip says "Offline since". Every feed polls within about a minute of the network's return | Finish any login page. Otherwise nothing |
| LLM errors while the network is up | After 2 minutes, classification uses keywords and extraction waits. The strip says "LLM degraded" | Nothing. It recovers by itself and catches up |
| Backlog lag over 2 minutes | The pipeline slows embedding and pauses backfills until backlog lag falls under 1 minute | Check `/status` for the slow stage |
| Free disk under 15 GB with no bucket configured, or under 5 GB with one | Original feed and page bodies are dropped, with no warning on the strip. Collection continues | Free space on the disk |
| Free disk under 10 GB | A yellow banner | `uv run monitor prune --dry-run --thin 15m --older-than 2h`, then run it without `--dry-run` |
| Free disk under 5 GB | A red banner, and full-text fetching stops | Prune as above. With a bucket configured, the pipeline offloads hourly, so `uv run monitor offload` frees little: it deletes only uploaded archive files at least 24 hours old |
| Free disk under 3 GB | The pipeline thins snapshots by itself | Free space elsewhere on the disk |
| A feed keeps failing | It retries about 15 minutes apart. `/status` shows it | Nothing |
| The dashboard feels slow | When uncached queries pass `WEB__SLOW_QUERY_MS` at p95, panels refresh at most every 20 seconds until p95 falls below half of it | Hide or close other dashboard windows. A hidden tab closes its stream and stops refreshing |
| The dashboard is down after an edit | Collection continues. A file with an error stops the dashboard until the file is saved again | Fix the file, or `git checkout` it |
| The dashboard is down | Collection continues | `uv run monitor web --reload` |
| The pipeline is down | Polling stops, and the strip reports it. On restart, time since the last fetch is recorded as a gap, and a gap of 5 minutes or more reads the news sitemaps after the first feed polls | `scripts/restart_pipeline.sh` |
| The strip says rows are held back | Commits are failing. The rows stay in memory and every commit retries them | Check free disk, and look for `flush_failed` in `logs/pipeline.jsonl` |
| The strip says rows are rejected | Rows the lake refused repeatedly are in `data/control/rejected-*.jsonl`, not in the lake. Later rows are unaffected | Look for `rows_rejected` in `logs/pipeline.jsonl`. The warning clears after the file is moved out of `data/control/` and the pipeline restarts |
| The strip says the archive writer stopped | Original feed and page bodies are not saved. Collection continues | Look in `logs/pipeline.jsonl`, then `scripts/restart_pipeline.sh` |
| The catalog is damaged | Queries fail | Stop the dashboard and the pipeline, then restore the newest backup, as described below |

### Restoring the catalog

Stop the dashboard first, with Ctrl-C in its terminal, and start it again
afterward with `uv run monitor web --reload`.

```bash
scripts/stop_pipeline.sh
uv run monitor restore
scripts/restart_pipeline.sh
```

`monitor restore` uses the newest backup whose catalog is intact and whose
Parquet files all exist, and names each newer backup it skipped and why.
Maintenance deletes replaced Parquet files after 2 hours and a prune deletes
them at once, so an older backup can be unusable. With the pipeline stopped,
`uv run monitor restore --dry-run` names the backup and leaves the catalog as
it was. The backup is decompressed and checked before the catalog moves. With
no usable backup, or a failure along the
way, the command exits with an error that names the problem, and the catalog
stays as it was.

The replaced catalog and its WAL stay beside it as
`data/catalog.sqlite.damaged-<time>`. Items committed after the backup are
fetched again if the feeds still list them, or if a news sitemap lists them
after a gap of 5 minutes or more. Model calls on those items are paid for
again, and ledger rows after the backup are gone, so the spend counters start
lower.

## After class

Reduce polling by raising `POLL__TIER_SECONDS`, for example to
`[180,600,1800]`, and restart the pipeline. Review disk use with `uv run
monitor disk` and `uv run monitor prune --dry-run`. If a bucket is configured,
`uv run monitor offload --lake` uploads every Parquet file on disk.
