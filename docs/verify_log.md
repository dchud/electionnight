# Verification log

Facts the spec marked for local checking, and where the system differs from
the spec's text. `monitor verify` rewrites rows V1 through V7.

| # | Check | Result |
|---|---|---|
| V1 | DuckDB, ducklake, and sqlite extension versions | pass 2026-11-01: duckdb 2.0.0, ducklake 417a9117f5, sqlite_scanner e223dc0149, DuckLake format 1.1 |
| V2 | Read-only attach works and rejects writes | pass 2026-11-01: `READ_ONLY` attach reads and rejects INSERT. It does not make arbitrary SQL safe: on a scratch lake, `ducklake_cleanup_old_files` and `ducklake_delete_orphaned_files` deleted Parquet files, and `COPY`, `EXPORT DATABASE`, a writable `ATTACH` of the catalog, and `DETACH` succeeded. The SQL console adds a parsed-statement allowlist and a macOS sandbox that denies network access, other programs, file writes, and reads under the home directory outside the lake and Python |
| V3 | Lake option names accepted | pass 2026-11-01: data_inlining_row_limit, parquet_compression, parquet_compression_level, target_file_size |
| V4 | Maintenance function names | pass 2026-11-01: ducklake_flush_inlined_data; ducklake_merge_adjacent_files; ducklake_expire_snapshots; ducklake_cleanup_old_files |
| V5 | Snapshot listing function | pass 2026-11-01: `lake.snapshots()` columns: ['snapshot_id', 'snapshot_time', 'schema_version', 'changes', 'author', 'commit_message', 'commit_extra_info'] |
| V6 | Embedding model available through fastembed | pass 2026-11-01: intfloat/multilingual-e5-small, custom ONNX export, returns 384 dimensions |
| V7 | Anthropic key, model ids, request settings, prompt caching | pass 2026-11-01: claude-haiku-5-5 parse with effort low and thinking disabled OK, cache read 2906 tokens on the second call; claude-sonnet-5-5 adaptive at medium OK; SDK 2.3.0 |
| V8 | htmx 2 SSE extension attribute syntax | pass 2026-11-01: htmx 2.0.11 and htmx-ext-sse 2.2.4: `hx-ext="sse"`, `sse-connect`, `sse-swap`, and `hx-trigger="sse:snapshot throttle:10s"`, exercised in a browser |
| V9 | Vendored asset versions | pass 2026-11-01: `scripts/vendor_assets.sh` pins htmx 2.0.11, sse 2.2.4, vega 6.4.0, vega-lite 6.4.1, vega-embed 7.3.0, d3 7.9.0, topojson-client 3.1.0, Inter 5.3.0, us-atlas 3.0.1, Tailwind 4.3.3 |
| V10 | OpenFEC endpoint and filters for 2026 candidates | pass 2026-11-01: `/v1/candidates/` honors `election_year`, `cycle`, `candidate_status`, `is_active_candidate`, `has_raised_funds`, `state`, `district`, `incumbent_challenge`. `election_type=G` is ignored, so Wikipedia candidate tables decide who is on the ballot |
| V11 | Race ratings sources reachable and parsed | pass 2026-11-01: Wikipedia `action=parse` on the 2026 Senate, governor, House, attorney general, secretary of state, lieutenant governor, and ballot measure pages. Sabato pages returned 403, so its ratings come from the Wikipedia column. Ratings not checked against raters' own sites |
| V12 | Poll-closing times confirmed | pass 2026-11-01: https://www.270towin.com/poll-closing-times/, cross-checked with The Green Papers and Wikipedia. Differs from the spec on KS, SD, ND, NH, ID, OR, AK; see `config/poll_closings.yaml` |
| V13 | Lean ratings confirmed with citations | owner confirmed national and analysis outlets 2026-11-01; 36 rows. State, local, and wire outlets stay `Unrated` |
| V14 | Battleground and close-race list confirmed | owner confirmed 2026-11-01: AK AZ FL GA IA KS ME MI MN NC NE NH NV NY OH OR PA SC TX WI, which is the derived list plus overrides for NC, AZ, PA, NY |

## Differences from the spec

- **Vectors.** DuckLake rejects `FLOAT[384]` with "unsupported type". Tables
  store `FLOAT[]`; queries cast to `FLOAT[384]` for array functions.
- **Embedding model.** fastembed does not list
  `intfloat/multilingual-e5-small`. The model's Hugging Face repository has
  `onnx/model.onnx`, registered with `TextEmbedding.add_custom_model`. Measured
  286 items per second on the CPU, with no torch install.
- **Storyline thresholds.** Baseline pairwise similarity over relevant items
  has p50 0.794, p90 0.825, p99 0.860, and p99.9 0.899, so the spec's 0.82 join
  collapses almost everything into one cluster: 99% of items at 0.84, 94% at
  0.86, and a 150-item cluster of unrelated stories at 0.90. At 0.92 the
  largest cluster has 9 items and sampled clusters each cover one story. Join
  is 0.92 and merge is 0.95, raised from 0.82 and 0.88 because merge has to
  exceed join. `scripts/tune_storylines.py` repeats the sweep.
- **Fetching.** The spec lists HTTP/2. Concurrent fetching with HTTP/2 raised
  h2 connection-state `ProtocolError`s, so fetching uses HTTP/1.1.
- **Catalog journal.** An open read on the catalog made a writer's commit fail
  with "database is locked". `monitor init` puts the catalog in WAL mode.
- **Soak test.** With WAL: 296 commits and 1145 reads over 10 minutes, 0
  errors, read p95 30 ms, commit p95 36 ms. Plans B and C were not needed.
- **Language detection.** `lingua-language-detector` adds 293 MB.
  `fasttext-wheel` is smaller but publishes no wheel for this platform, so
  `lingua` is used.
- **Dependencies.** DuckDB's Python client requires `pytz` for `TIMESTAMPTZ`.
- **Budget caps.** `LLM__HARD_USD_CAP` applies to total spend across all days.
- **Configuration.** Tunables are environment variables in `.env`. There is no
  `config/settings.toml`.
- **Frontend versions.** Altair 6 generates Vega-Lite 6 specs, so the
  dashboard uses Vega 6 and Vega-Lite 6 rather than 5.
- **Dashboard queries.** Panels query an in-memory fact table in the web
  process, because every panel honors Focus, Level, Language, and Unique, which
  pre-aggregated bins cannot serve. `gold.*` is still computed.
- **Time axis.** On the dashboard, an item published more than 2 hours before
  it was first seen sits at its published time, so a feed's first fetch does
  not appear as one spike. An item published during a collection gap and first
  seen within 15 minutes after it sits there too. `gold.*` bins use
  `first_seen_at` unchanged.
- **Laptop sleep.** The system runs under `caffeinate`. The owner closes the
  lid to travel to class, which sleeps the machine anyway. `time.monotonic`
  here is `mach_absolute_time()`, which stops during sleep, so timers resume
  instead of firing late, and the pipeline detects sleep from the difference
  between it and the wall clock. Network loss is confirmed by HTTPS requests to
  three hosts, because a Wi-Fi login page cannot pass a certificate check.
- **News sitemaps.** The spec collects from feeds only. After a gap, collection
  also reads Google News sitemaps, listed in `config/news_sitemaps.csv` by
  `scripts/find_news_sitemaps.py`. A sitemap is listed only if the outlet's
  robots.txt names it and allows this crawler. Tested 2026-11-01: 128 of 269
  enabled outlets qualify. The owner chose catch-up only, so outlets with no
  feed stay out.
- **Tone intervals.** The spec gives a mean shrunk toward 0 with 5
  pseudo-observations and a 95% interval from the standard error. Simulated
  coverage of that interval, with a true mean of -1.0 and a standard deviation
  of 0.5: 32% at n=6, 44% at n=14, 65% at n=30, 83% at n=100. With a true mean
  of 1.5 and n=8 it is 0%. The dashboard draws the unshrunk mean with a 95%
  t-interval, which covers 95% at those counts. The hide and low-n rules stay.
- **Replay.** Spec section 9.5 takes storyline assignment as of the replay
  time. The read model keeps one row per item, holding its latest assignment,
  classification, and tone, and a storyline's latest label. Replay filters on
  true first-seen time. The attention baseline, the active storyline count, and
  the status that admits a storyline to Rising are as of the replay time. The
  rest is current, and the replay header says so. Feed health is not shown for
  a past moment.
- **Rising Storylines.** The spec ranks cards by velocity score. The panel
  lists only storylines that meet the rising rule, and says so when none do.
- **Stream colors.** No palette of 10 hues avoiding party blue and red passes
  the colorblind checks. The first 6 are validated; the rest rely on direct
  labels and hover.
- **Replaced files.** Spec section 8.3 expects files replaced by compaction to
  be held by retained snapshots. A merged file keeps each row's snapshot range,
  so time travel works without them; a scratch-lake test confirmed
  `AT (VERSION => n)` is exact after deleting them, with no snapshot expired.
  Maintenance deletes replaced files after 2 hours. Without this, each merge
  left a full copy behind: 228 MB on disk against 38 MB live after 2 hours.
- **Local NER fallback.** spaCy is not installed. Spec section 17.2 has Stage B
  queue during an API outage and catch up, so the fallback would not run.
- **Entity Rank labels.** Spec section 14.3 puts a label at each line end.
  Lines that end at one rank in different bins can overlap, so each rank names
  only the line that ended last. Hover names the other lines, even when their
  ends are far apart.
- **Ad Fontes.** Spec sections 5.3, 5.4, and 14.4 use Ad Fontes Media bias and
  reliability scores. The config and the reference pages carry none, and the
  criteria on `/sources` do not name it. Its terms of use bar AI agents from
  extracting its content and grant no right to republish it, and no metric or
  chart read the scores. The lake table keeps its three columns. A lake that
  loaded the earlier file keeps those values in older rows, because the lake is
  append-only.
