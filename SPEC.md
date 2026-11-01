# Election Night News Monitor — Build Spec (v0.9 draft)

> **Audience:** Claude Code running on the owner's Mac, plus the owner.
> **Election:** Tuesday, November 3, 2026 (US midterms: House, ~35 Senate seats, 36 governors, state/local races, ballot measures).
> **Spec written:** Sunday, November 1, 2026. **Hard deadline:** code freeze Tue Nov 3, 3:00 pm ET.
> **Companion file:** `sources_seed.csv` (seed outlet list; feed URLs to be discovered and validated locally).

---

## 0. Instructions for Claude Code (read first)

1. **Copy this section into the repo's `CLAUDE.md`** at scaffold time. Keep `SPEC.md` in the repo root as the source of truth.
2. **Work in the phase order in §19.** Each phase has acceptance criteria. Don't start a later phase until the earlier one's criteria pass, unless the owner says so. Collecting a baseline early matters more than polish.
3. **Items tagged `VERIFY-LOCAL`** are facts the spec author couldn't confirm: library APIs, option names, feed URLs, election facts. Before you rely on one, check it (docs, `pip show`, a small test script, or a web search) and record the result in `docs/verify_log.md`. If reality differs from the spec, follow reality and note the difference.
4. **Single-writer rule.** Only the `pipeline` process writes to the DuckLake. The `web` process never writes to the lake (its own UI-preferences DB is fine).
5. **Append-only by default.** Never UPDATE or DELETE rows in lake tables during the event. Corrections go in as new rows with a newer `created_at`. Readers take the latest row. This keeps snapshots cheap and replay correct.
6. **Respect the disk budget (§16).** Check free space before downloading models or data. If free disk drops below 8 GB, stop and tell the owner.
7. **Prefer boring and robust.** Deadline beats features. When a stretch item threatens a core item, drop the stretch item and say so.
8. **Claude API:** use the official `anthropic` Python SDK. Before writing API code, consult the `claude-api` skill (or the live docs) for current request shapes: structured outputs, thinking/effort settings, prompt caching. Don't guess parameters. Keep model IDs in config.
9. **Never commit secrets.** `ANTHROPIC_API_KEY` and optional `FEC_API_KEY` come from the environment or `.env`, which is git-ignored.
10. **Vendor every frontend asset locally** (no CDNs at runtime). The classroom network may be unreliable.

---

## 1. Goals and non-goals

### Goals
- Continuously ingest 250–400 reputable news feeds: US national, state and local, international English, and a small sample in the other five UN languages. Start now (baseline), then run intensively 3 pm ET Nov 3 → ~2 am ET Nov 4.
- Store everything in a **DuckLake** (DuckDB-queryable, Parquet-backed, snapshot-versioned) on the local Mac.
- Run a **series of modular parsers** over the stories:
  - election relevance and level (national / state / local);
  - topics;
  - people, organizations, parties, offices and ballot measures;
  - place names resolved to FIPS codes;
  - overall article tone, and tone toward specific entities;
  - multilingual vector embeddings;
  - storyline clustering;
  - race calls and results as reported in the news.
- Serve a **FastAPI + HTMX + Tailwind dashboard**:
  - updates in real time;
  - shows short-term (15 min) and medium-term (2–6 h) trends;
  - supports drill-down to every story, entity, state and storyline;
  - carries **citations everywhere**, plus reference pages for sources and methods.
- Make it projector-friendly (1280–2560 px wide), with easy **focus modes**: battlegrounds, close races, custom watchlist, single state.
- Keep the data afterwards with compressed snapshots, and give the owner a one-command prune.

### Non-goals
- Official results or vote counts. No paid APIs (AP Elections API etc.). Results appear only as *claims reported in news*, always cited and labeled as such.
- Phone layouts.
- Multi-user or auth. Everything runs on localhost (optionally LAN-visible for a second screen).
- Fringe or hyper-partisan outlets (exclusion criteria in §5.3).

---

## 2. Operating timeline (all times ET)

| When | What |
|---|---|
| Sun Nov 1 (evening) | Phase 0: scaffold, validate feeds, start the poller. **Baseline collection begins.** |
| Mon Nov 2 | Phases 1–2: enrichment, rollups, dashboard core. Pipeline keeps running. |
| Tue Nov 3, morning | Phase 3: deep extraction, drill-downs, citations, reference pages. |
| Tue Nov 3, 12:00–3:00 pm | Phase 4: hardening, dry run in replay mode, run-of-show. **Freeze at 3:00 pm.** |
| Tue Nov 3, 3:00 pm → Wed ~2:00 am | Live operation. Poll closings 6 pm–1 am ET (`config/poll_closings.yaml`). |
| Wed Nov 4 onward | Keep running at a slower cadence if wanted. Post-election analysis, replay for class. |

---

## 3. Platform constraints

- **MacBook Air M4, 24 GB RAM, fanless.** Sustained local ML load will throttle. So:
  - Use small local models only: one embedding model, plus tiny spaCy models as fallback.
  - Push language understanding to the **Claude API (Haiku 5.5)**, which is cheap enough to run on every item (§12).
  - Batch local inference. Cap the embedding worker at roughly 30–50% duty cycle.
- **Disk is tight.** Target **≤ 8 GB total footprint**: code, venv, models, data, snapshots (§16). Prefer ONNX runtimes over PyTorch where possible.
- **Projector output.** Design for 1920×1080 first. Test at 1280×720 (projector fallback) and 2560×1600 (laptop Retina at default scaling).

---

## 4. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Python | 3.12, managed with **uv** | `uv init`, `uv add …`, `uv run …` |
| Storage | **DuckDB** (latest stable) + **ducklake** extension + **sqlite** extension | Catalog in SQLite, data in Parquet. `VERIFY-LOCAL`: current versions and that DuckLake is stable on them. |
| Fetching | `httpx[http2]` (async), `feedparser`, `trafilatura` | Conditional GET, per-domain rate limits |
| Language ID | `lingua-language-detector` (low-accuracy mode) or `fasttext` lid.176 (small) | Pick the smaller install |
| Embeddings | **`intfloat/multilingual-e5-small`** (384-dim), run via **fastembed / ONNX** if supported, else `sentence-transformers` on MPS | `VERIFY-LOCAL`: does fastembed support this model? If not, pick fastembed's closest multilingual 384-dim model (e.g. `paraphrase-multilingual-MiniLM-L12-v2`), or fall back to sentence-transformers + torch (+~1.5 GB disk). |
| Local NER fallback | spaCy `xx_ent_wiki_sm` (multilingual) + `en_core_web_sm` | Used only when the Claude API is unavailable |
| Fuzzy matching | `rapidfuzz` | Entity aliasing, place matching |
| LLM | `anthropic` SDK; `claude-haiku-5-5` (bulk), `claude-sonnet-5-5` (briefs, hard cases) | See §12 |
| Web | FastAPI, uvicorn, Jinja2, `sse-starlette` | |
| Frontend | HTMX 2 + htmx SSE extension, **Tailwind v4 standalone CLI** (no Node), Vega 5 / Vega-Lite 5 / vega-embed, D3 v7, topojson-client, us-atlas (pre-projected Albers TopoJSON) | All vendored into `web/static/vendor/`. `VERIFY-LOCAL`: exact versions; download once with a script. |
| Charts (server-side specs) | **Altair** generates Vega-Lite JSON specs; the browser streams data into them | |
| Config | `pydantic-settings` + TOML/YAML/CSV files | |
| Logging | `structlog` → JSON lines in `logs/` | |
| Process management | `honcho` with a `Procfile` (pipeline + web + tailwind watch in dev) | |

---

## 5. Sources

### 5.1 Files
- `config/sources.csv` is canonical. It is generated from `sources_seed.csv` by the feed-discovery step and then hand-edited. Columns:
  - `source_id` (slug), `name`, `homepage`, `feed_urls` (pipe-separated);
  - `scope` (`wire|national|international|state|local|analysis`), `home_state` (USPS code or blank), `country`, `language` (ISO 639-1);
  - `lean_label` (`L|LL|C|LR|R|NA|Unrated`), `lean_source`, `lean_source_url`, `lean_retrieved_on`;
  - `reliability_note`, `ownership`, `state_affiliated` (bool), `paywall` (`none|metered|hard`);
  - `poll_tier` (1=60 s, 2=180 s, 3=600 s), `enabled` (bool), `notes`.
- `monitor sources export-opml` / `import-opml` round-trips to OPML. The owner can then curate in **NetNewsWire**: import the OPML into a category, read and prune, export, re-import. NetNewsWire is a curation and reading UI only, never the fetcher.

### 5.2 Feed discovery and validation (Phase 0 task, run locally)
For each seed row, find feeds in this order:
1. Known feed URL patterns for the outlet (politics or elections section feeds preferred over the front page).
2. `<link rel="alternate" type="application/rss+xml|atom+xml">` on the homepage and on the `/politics` / `/elections` section pages.
3. Common paths (`/feed`, `/rss`, `/rss.xml`, `/feeds/…`, `/arc/outboundfeeds/rss/…`).

Validate each candidate: HTTP 200, parses with feedparser, ≥ 5 entries, newest entry < 72 h old (< 7 days for small local outlets).

- Keep up to 3 feeds per outlet: e.g. politics + elections + top stories.
- Write `reports/feed_validation.md` listing failures. **Ask the owner** before replacing any seed outlet.
- Target after validation: ≥ 250 healthy outlets; ≥ 2 per state (≥ 4 in battleground states); ≥ 3 per non-English language.
- Known issue: Reuters and AP offer little or no public RSS. Wire copy will arrive through syndicating outlets, which dedupe handles (§9.3). Don't scrape aggregators.

### 5.3 Inclusion / exclusion criteria (for the reference page)
- **Include:** established newsrooms with editorial standards and corrections policies, across a modest spectrum (Left through Right per AllSides, centered on Lean Left / Center / Lean Right); nonprofit state newsrooms; public broadcasters with editorial independence.
- **Exclude:**
  - outlets rated low-reliability / "problematic" by Ad Fontes Media or similar;
  - conspiracy or fabrication-prone sites;
  - state-controlled media of authoritarian governments (e.g. RT, Sputnik). `state_affiliated=true` outlets of that kind are omitted from the seed (see open question Q3).
  - pure opinion aggregators;
  - partisan campaign or party sites.

### 5.4 Lean labels and citations
- Primary label: **AllSides Media Bias Rating** (5-point scale) where one exists. Secondary: **Ad Fontes** bias and reliability scores where available.
- Store the rating, a URL to the rating page, and a retrieval date.
- **Don't bulk-scrape** rating sites. Populate `config/lean_ratings.csv` semi-manually: Claude Code may web-search each outlet's rating page and propose values for the owner to confirm.
- `sources_seed.csv` includes a `lean_hint` column: the spec author's rough expectation. It is a starting point only and **must be checked** before display (`VERIFY-LOCAL`).
- Outlets with no rating → `Unrated`, shown in neutral gray.
- International and non-English outlets → `NA`: the US left/right scale doesn't transfer. Show "Non-US outlet" instead.

### 5.5 Non-English sample (UN languages)
About 4–6 outlets each for Arabic, Chinese, French, Russian and Spanish (Spanish also includes US Spanish-language outlets). Prefer independent and public-service outlets (e.g. BBC language services, DW, RFI, France 24, Lianhe Zaobao, Meduza). Titles and summaries are translated to English by Haiku in Stage A (§10).

---

## 6. Repository layout

```
election-monitor/
  CLAUDE.md                 # copy of §0 + conventions
  SPEC.md
  Procfile                  # pipeline, web, css
  pyproject.toml / uv.lock
  .env.example              # ANTHROPIC_API_KEY=, FEC_API_KEY=
  config/
    settings.toml           # all tunables (cadences, thresholds, model ids, budgets, paths)
    sources.csv
    lean_ratings.csv
    taxonomy.yaml           # topics, race types, levels (§11)
    races.yaml              # race watchlist + ratings (§11.3)
    poll_closings.yaml      # per-state close times (§11.4)
    election_keywords.yaml  # multilingual fallback keyword lists
  data/                     # git-ignored
    catalog.sqlite          # DuckLake catalog
    lake/                   # Parquet data files
    ref/                    # gazetteers, topojson, candidate roster (reference data)
    ui.sqlite               # web-owned prefs (watchlist edits, saved views)
    backups/                # rotating catalog backups
    models/                 # model cache (HF_HOME / FASTEMBED cache pointed here)
  src/monitor/
    cli.py                  # `monitor …` (Typer)
    settings.py
    lake.py                 # connection helpers, attach, maintenance
    ingest/{poller.py, feeds.py, extract.py, dedupe.py}
    parsers/                # one module per parser (§10), common interface
      base.py
      lang.py classify_a.py extract_b.py entities.py geo.py
      embed.py storylines.py tone_local.py race_calls.py
    rollups.py
    llm/{client.py, prompts/, schemas.py, budget.py}
    ref/{gazetteer.py, candidates.py, races.py}
    web/
      app.py  routes/ (pages.py, fragments.py, api.py, sse.py)
      queries/ (*.sql — named, parameterized)
      charts/ (altair spec builders)
      templates/ (base.html, pages/, fragments/)
      static/ (css/, js/, vendor/, topo/)
  scripts/                  # one-off: vendor_assets.sh, build_ref_data.py, discover_feeds.py
  tests/                    # fixtures/, test_*.py
  reports/                  # feed_validation.md, run logs
  docs/                     # verify_log.md, run_of_show.md, methods.md (also rendered in-app)
```

---

## 7. Architecture

```
            config/sources.csv
                    │
          ┌─────────▼──────────┐    tiered polling 60/180/600 s, ETag/Last-Modified,
          │  Poller (asyncio)  │    per-domain concurrency=2, jitter, backoff
          └─────────┬──────────┘
                    │ new entries
          ┌─────────▼──────────┐
          │  Stage queue       │  in-process asyncio queues per stage;
          │  (pipeline proc)   │  durable state = lake tables + stage watermarks
          └─┬───────┬───────┬──┘
     lang+A │  embed│  dedupe/storylines   B (full text → deep extraction, relevant only)
            ▼       ▼       ▼
          ┌──────────────────────┐  micro-batch commits every 30 s (one transaction per batch)
          │  DuckLake writer     │──► data/catalog.sqlite + data/lake/*.parquet (zstd)
          └─────────┬────────────┘
                    │ rollups every 30–60 s (gold.*), maintenance every 15 min
                    ▼
          ┌──────────────────────┐  read-only attach; polls latest snapshot id every 3 s;
          │  web (FastAPI)       │  SSE "snapshot" events → HTMX/Vega refresh
          └──────────────────────┘
```

**Processes** (via `honcho start`):
- `pipeline`: `uv run monitor pipeline`. The only lake writer. Hosts the poller, all parser stages, rollups, maintenance and the LLM client.
- `web`: `uv run monitor web --host 127.0.0.1 --port 8000`. Read-only.
- `css` (dev only): Tailwind standalone watch.

**Concurrency with DuckLake.** The SQLite catalog lets several local processes attach. The web process attaches **read-only**. `VERIFY-LOCAL`:
- The exact read-only attach syntax, e.g. `ATTACH 'ducklake:sqlite:data/catalog.sqlite' AS lake (DATA_PATH 'data/lake/', READ_ONLY)`.
- That concurrent reads during writer commits don't error. Write a 2-process soak test: a writer committing every 2 s and a reader querying every 0.5 s for 10 minutes.
- If contention shows up, enable SQLite WAL mode on the catalog file and add reader retry with backoff.
- **Plan B** if still flaky: Postgres.app as the catalog (`ducklake:postgres:…`).
- **Plan C:** the writer exports `gold.*` to Parquet files after each rollup and the web reads those.

**Stage state.** Each parser records `(item_id, parser, parser_version, status, created_at)` in `nlp.parser_runs`. On restart, a stage resumes from items lacking a successful run for its current version. Bumping `parser_version` re-processes in the background (rate-limited).

**Change notification.** The web process polls `SELECT max(snapshot_id) FROM lake.snapshots()` (`VERIFY-LOCAL`: function name) every 3 s. On change it emits SSE `snapshot` with `{snapshot_id, committed_at, changed: [table names]}`. The writer also writes a tiny `data/last_commit.json` as a cheap fallback signal.

---

## 8. Storage: DuckLake configuration and lifecycle

### 8.1 Setup
- One DuckLake named `lake`. Catalog `data/catalog.sqlite`, data path `data/lake/`.
- Schemas: `raw`, `nlp`, `gold`, `ref`. (`ref` holds reference tables: gazetteer, candidates, races, sources mirror.)
- Options (`VERIFY-LOCAL`: exact option names, via `CALL lake.set_option(...)` or the documented equivalent; record the actual names in VERIFY_LOG):
  - Parquet compression **zstd** (level ~6–9);
  - target file size ~64–128 MB;
  - **data inlining** for small inserts (row limit ~1000–5000), so 30-second micro-batches don't create thousands of tiny files;
  - timezone handling: store `TIMESTAMPTZ` in UTC.

### 8.2 Maintenance (pipeline, every 15 min; skip if a commit is in flight)
- Flush inlined data to Parquet when inlined rows exceed the threshold. `VERIFY-LOCAL`: `ducklake_flush_inlined_data` or equivalent.
- Merge adjacent small files (`ducklake_merge_adjacent_files` or `CHECKPOINT`; `VERIFY-LOCAL`).
- **Don't expire snapshots automatically** (owner wants them kept). Exception: the disk guard in §8.4.
- Back up the catalog every 15 min: SQLite online backup to `data/backups/catalog-YYYYmmdd-HHMM.sqlite.zst`. Keep the last 12 plus one per hour. Catalog corruption is the single fatal failure mode, so this is not optional.

### 8.3 Why keeping snapshots is cheap here
Tables are append-only, so each snapshot mostly references files that are still live. Old files are retained only after compaction rewrites them. Disk growth from retained snapshots is therefore roughly "data size × (1 + compaction churn)". `monitor disk` reports this.

### 8.4 Pruning CLI (owner-facing; must be simple)
```
monitor disk                         # breakdown: lake live data, retained-by-snapshots, catalog, backups, models, logs; free space
monitor prune --thin 15m --older-than 2h   # keep 1 snapshot per 15 min for anything older than 2 h
monitor prune --before "2026-11-03 12:00"  # expire all snapshots before a time (data rows are kept!)
monitor prune --all-but-latest             # keep only the current snapshot
monitor prune --backups 3                  # keep newest 3 catalog backups
monitor prune --dry-run …                  # show what would be freed
```
- After expiring snapshots, run the old-file cleanup (`ducklake_expire_snapshots(... versions => [...] | older_than => ...)`, then `ducklake_cleanup_old_files`; `VERIFY-LOCAL`), then `VACUUM` the catalog.
- Prune **never deletes current data rows**. Replay keeps working after any prune, because replay is time-filtered (§9.5), not snapshot-based.
- **Disk guard:**
  - Free < 10 GB → yellow banner in the dashboard.
  - Free < 5 GB → red banner, and the pipeline stops full-text fetching (Stage B uses summaries only).
  - Free < 3 GB → automatically run `--thin 15m --older-than 1h`, and log loudly.

---

## 9. Data model

Types: `TEXT`, `TIMESTAMPTZ` (UTC), `FLOAT`, `INT`, `BOOLEAN`, `FLOAT[384]`, `TEXT[]`, `JSON`.
Every table has `created_at TIMESTAMPTZ` (write time). IDs are deterministic hashes (xxhash64 hex) so reprocessing is idempotent.

### 9.1 `raw`
- **`raw.fetches`**: one row per HTTP fetch.
  - `fetch_id`, `source_id`, `feed_url`, `started_at`, `duration_ms`, `http_status`, `not_modified BOOL`, `n_entries`, `n_new`, `error`.
- **`raw.items`**: one row per distinct canonical URL.
  - `item_id` (hash of canonical URL), `source_id`, `feed_url`, `url`, `canonical_url`, `guid`;
  - `title`, `summary` (HTML stripped, ≤ 2000 chars), `author`, `published_at` (from feed; nullable), `first_seen_at` (when we first saw it; **the primary time axis**);
  - `language`, `feed_categories TEXT[]`, `raw_entry JSON` (trimmed).
- **`raw.article_text`**: full text, fetched only for Stage-A-relevant items.
  - `item_id`, `fetched_at`, `http_status`, `text` (≤ 20k chars), `n_chars`, `extractor` ('trafilatura'), `paywalled BOOL`.

### 9.2 `nlp` (one table per parser; all carry `parser`, `parser_version`)
- **`nlp.parser_runs`**: `item_id`, `parser`, `parser_version`, `status` (`ok|error|skipped`), `error`, `created_at`.
- **`nlp.lang`**: `item_id`, `language`, `confidence`.
- **`nlp.classify_a`**: Stage A, run on every item over title + summary.
  - `item_id`, `election_relevant BOOL`, `relevance_conf FLOAT`, `us_politics BOOL`;
  - `levels TEXT[]`, `primary_level`, `states TEXT[]`, `race_types TEXT[]`, `topics TEXT[]`;
  - `is_results_report BOOL`, `headline_en`, `summary_en` (translations for non-English; null for English), `method` (`llm|keywords`).
- **`nlp.extract_b`**: Stage B, run on relevant items over full text (or summary).
  - `item_id`, `summary_en` (1–2 sentences), `article_tone FLOAT` (−2..+2), `topics_refined TEXT[]`, `raw JSON` (full validated LLM output).
- **`nlp.mentions`**:
  - `mention_id`, `item_id`, `surface`, `char_start`, `char_end` (nullable; located by string search), `ent_type` (`PERSON|ORG|PARTY|OFFICE|MEASURE|GPE|EVENT`);
  - `role` (`candidate|official|election_official|party|org|measure|other`), `entity_id` (nullable until resolved), `tone FLOAT` (nullable), `tone_evidence TEXT` (short quote).
- **`nlp.places`**:
  - `item_id`, `surface`, `place_type` (`state|county|city|cd|other`);
  - `state_usps`, `county_fips`, `place_geoid`, `cd_code` (e.g. `TX-35`), `match_score`, `method` (`gazetteer|llm|source_home`).
- **`nlp.embeddings`**: `item_id`, `model`, `vec FLOAT[384]`, `text_basis` (`title+summary|title+summary+lead`).
- **`nlp.dup_groups`**: `item_id`, `dup_group_id`, `is_canonical BOOL`, `method` (`url|title_hash|embedding`).
- **`nlp.storyline_assign`** (append-only; latest row per item wins):
  - `item_id`, `storyline_id`, `similarity`, `assigned_at`.
- **`nlp.storylines`** (append-only versions; latest per `storyline_id` wins):
  - `storyline_id`, `version`, `label` (≤ 8 words), `summary` (≤ 2 sentences with footnote ids), `keywords TEXT[]`;
  - `centroid FLOAT[384]`, `first_seen_at`, `n_items`, `n_sources`, `states TEXT[]`, `primary_level`, `status` (`candidate|active|dormant|merged`), `merged_into`.
- **`nlp.race_calls`**: claims in news, not official.
  - `call_id`, `item_id`, `race_key`, `race_desc`, `winner_name`, `winner_entity_id`, `party`;
  - `called_by` (`AP|NBC|CNN|Fox|DDHQ|ABC|CBS|NYT|other|unspecified`), `status` (`projected|declared|conceded|leading|too_close|uncalled`), `claimed_at_text`, `confidence`.
- **`nlp.briefs`**: Sonnet-written evening narratives.
  - `brief_id`, `window_start`, `window_end`, `focus` (`all|battleground|state:XX`), `markdown` (with `[^n]` footnotes), `citations JSON` (n → item_id), `model`.
- **`nlp.llm_calls`**: cost ledger.
  - `call_id`, `purpose`, `model`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `usd_est`, `latency_ms`, `status`, `created_at`.

### 9.3 Dedupe and syndication
1. Canonicalize URLs: lowercase host; strip `utm_*`, `fbclid`, `gclid`, `cmpid`, `amp` paths and fragments; prefer `<link rel=canonical>` when the article was fetched.
2. Within 36 h, a near-duplicate is a normalized-title SimHash distance ≤ 3, or embedding cosine ≥ 0.95 with the same `published_at` day. Mark it in `nlp.dup_groups`.
3. Metrics count **unique stories** (canonical members) by default, with a UI toggle for "all copies (incl. syndication)". Outlet-level metrics (e.g. coverage by lean) count all copies, because republishing *is* that outlet's coverage choice.

### 9.4 `ref`
- `ref.sources`: mirror of `config/sources.csv` + `lean_ratings.csv`, reloaded on change.
- `ref.states`: `usps`, `fips`, `name`, `battleground BOOL`, `poll_close_et`, `poll_close_all_et`.
- `ref.counties`, `ref.places`: Census Gazetteer.
- `ref.entities`: `entity_id`, `canonical_name`, `ent_type`, `party`, `state`, `office`, `race_key`, `incumbent BOOL`, `wikidata_qid`, `aliases TEXT[]`.
- `ref.races`: `race_key` (e.g. `2026-GA-SEN`, `2026-AZ-GOV`, `2026-PA-H07`, `2026-CA-M-<slug>`), `state`, `office`, `district`, `rating`, `rating_source`, `rating_url`, `close_race BOOL`, `candidates TEXT[]` (entity_ids), `watch BOOL`.

### 9.5 `gold` (rollups, rebuilt incrementally every 30–60 s for recent bins)
Bins are 5-minute buckets of `first_seen_at`, keyed `bin_start`. Every table has `unique_only BOOL` variants or columns.
- `gold.bin_counts`: `bin_start`, `level`, `n_items`, `n_relevant`, `n_unique`.
- `gold.storyline_bins`: `bin_start`, `storyline_id`, `n_items`, `n_unique`, `n_sources`, `mean_tone`.
- `gold.entity_bins`: `bin_start`, `entity_id`, `n_mentions`, `n_items`, `tone_sum`, `tone_sq_sum`, `tone_n`.
- `gold.entity_lean_bins`: as above, by `lean_label`.
- `gold.state_bins`: `bin_start`, `state_usps`, `n_items`, `n_unique`, `tone_sum`, `tone_n`.
- `gold.county_bins`: `bin_start`, `county_fips`, `n_items`.
- `gold.source_bins`: `bin_start`, `source_id`, `n_items`, `n_relevant`.
- `gold.lean_storyline_bins`: `bin_start`, `storyline_id`, `lean_label`, `n_items`.
- `gold.comention_edges`: `bin_start`, `entity_a`, `entity_b`, `n` (stretch).
- `gold.topic_bins`: `bin_start`, `topic`, `n_items`.

**Replay and time travel.** Every dashboard query takes an `as_of` parameter (default now) and filters `bin_start + 5min <= as_of` / `first_seen_at <= as_of`. Storyline assignment uses the latest row with `assigned_at <= as_of`. This gives exact replay with no snapshots needed. DuckLake snapshots remain available for forensic `AT (VERSION => …)` queries.

---

## 10. Parser pipeline (the "series of parsers")

**Common interface** (`parsers/base.py`):
```python
class Parser(Protocol):
    name: str; version: str
    input_query: str          # SQL selecting item_ids needing this parser (by version)
    batch_size: int; max_concurrency: int
    async def run(self, batch: list[Item]) -> ParserOutput  # rows per target table
```
The orchestrator pulls batches, runs them, and hands rows to the writer for the next 30-second micro-commit. A failure marks `parser_runs.status='error'` and retries with backoff (max 3).

**Stages, in dependency order.** Latency target: an item is visible on the dashboard ≤ 90 s after it is first seen.

| # | Parser | Input | Method | Output tables | Notes |
|---|---|---|---|---|---|
| P1 | `lang` | title+summary | lingua/fasttext | `nlp.lang` | Trust the feed's declared language if it's consistent |
| P2 | `classify_a` | title+summary (+feed categories, source scope/home_state) | **Haiku 5.5**, structured output, batched 10 items/call | `nlp.classify_a` | Runs on **all** items. Translates non-English headline + summary. Fallback: keyword rules from `election_keywords.yaml` (`method='keywords'`). |
| P3 | `embed` | `passage: ` + title(_en) + summary(_en) [+ first 600 chars of text when available] | multilingual-e5-small (ONNX/MPS), batch 32 | `nlp.embeddings` | Runs on all items (baseline semantics). Re-embed with lead text after Stage B? No: keep one embedding per item for simplicity. |
| P4 | `dedupe` | URL, title, embedding | §9.3 | `nlp.dup_groups` | |
| P5 | `fetch_text` | relevant items (`relevance_conf ≥ 0.5`) | trafilatura; per-domain 1 req / 3 s; robots.txt respected; 10 s timeout | `raw.article_text` | Skip `paywall=hard` sources (use summary). Disk guard can disable. |
| P6 | `extract_b` | full text (≤ 6k tokens, truncated at a paragraph boundary) or summary | **Haiku 5.5**, structured output, 1 item/call, concurrency 8 | `nlp.extract_b`, `nlp.mentions`, `nlp.places` (llm), `nlp.race_calls` | Entities with roles + tone toward each + evidence quote; places; races; race-call claims; article tone |
| P7 | `entities` | mentions | alias match against `ref.entities` (exact → normalized → rapidfuzz ≥ 92 within same state/race context) | updates via new rows in `nlp.mentions` (resolved copy) | Every 5 min, the top 30 unresolved PERSON surfaces by frequency go to Haiku in one call for resolution / new-entity proposals → `ref.entities` (ref tables may be updated by the pipeline) |
| P8 | `geo` | `nlp.places` (llm) + source home_state | Gazetteer match; ambiguous names resolved by (a) state in the same item, (b) source home_state, (c) population prior | `nlp.places` (`method='gazetteer'`) | Congressional districts: accept `cd_code` from the LLM when formatted `XX-NN` |
| P9 | `storylines` | embeddings of relevant canonical items | Online clustering (below) | `nlp.storyline_assign`, `nlp.storylines` | Haiku names and summarizes storylines on promotion and on growth |
| P10 | `tone_local` (fallback only) | text | none (tone = null) or a small local model if disk allows | — | If the API is down, tone panels show "paused" rather than bad numbers |
| P11 | `rollups` | all of the above | SQL | `gold.*` | Every 30–60 s for the last 2 h of bins; full rebuild nightly |
| P12 | `briefs` | gold + top storylines + top items | **Sonnet 5.5**, every 20 min from 4 pm ET Nov 3 (hourly otherwise), plus on demand | `nlp.briefs` | Must cite item ids; validator drops uncited sentences |

### 10.1 Storyline clustering (P9)
- Candidate pool: storylines with `status in (candidate, active)` and last item within 8 h.
- For each new relevant canonical item with embedding *v*: find the max cosine against centroids.
  - If ≥ **τ_join = 0.82** (`VERIFY-LOCAL`: tune on the Sunday–Monday baseline; log similarity histograms), assign it and update the centroid as an EMA (α = 0.1), then re-normalize.
  - Otherwise create a `candidate` storyline.
- **Promotion** to `active`: ≥ 3 unique items from ≥ 2 sources within 3 h.
- **Merge pass** every 10 min: active centroids with cosine ≥ 0.88 merge into the older one (`status='merged'`, `merged_into`).
- **Dormancy:** no items for 4 h → `dormant`, which can be revived by a join.
- **Naming:** on promotion, and when `n_items` grows by 50% since the last label, send Haiku the 8 most central headlines (English) plus keywords (c-TF-IDF over titles). Output: `label` (≤ 8 words, neutral wording, no party framing) + `summary` (≤ 2 sentences, footnoted to supplied item ids).

### 10.2 Classification details (P2)
- **Levels:**
  - **National:** control of Congress, national party figures, nationwide themes, federal election administration.
  - **State:** statewide races (Senate, governor, AG, SoS), state legislatures, statewide ballot measures, state election administration.
  - **Local:** House districts, county/municipal races, local measures, county-level election operations (polling places, lines, counting).
  - Multi-label is allowed; `primary_level` is required.
- Haiku receives source metadata (scope, home_state) as context. A local outlet's story about its own House race → local + state.
- `election_relevant` = about the 2026 US elections: campaigns, races, voting, results, administration, legal challenges, turnout, the post-election transition of power. US policy news with no electoral angle is `us_politics=true, election_relevant=false`.

---

## 11. Reference data

### 11.1 Taxonomy (`config/taxonomy.yaml`)
**Topics** (closed set; the LLM must choose from it, plus up to 3 free tags):
- `control_senate`, `control_house`, `governor_races`, `senate_race`, `house_race`, `state_legislature`, `local_race`;
- `ballot_measure_abortion`, `ballot_measure_redistricting`, `ballot_measure_elections`, `ballot_measure_economy`, `ballot_measure_drugs`, `ballot_measure_other`;
- `turnout`, `early_vote_mail`, `polling_place_ops`, `lines_wait_times`, `voting_hours_extended`, `machine_or_tech_issue`, `vote_counting`;
- `results_projection`, `concession_victory`, `recount_close_race`, `legal_challenge`;
- `misinformation_claims`, `election_security_threats`, `violence_intimidation`, `foreign_interference`;
- `exit_polls`, `polls_forecasts`;
- `issue_economy`, `issue_immigration`, `issue_abortion`, `issue_health`, `issue_crime`, `issue_democracy`, `issue_foreign_policy`;
- `campaign_finance`, `candidate_controversy`, `endorsements`, `redistricting_maps`;
- `trump_factor`, `party_strategy`, `international_reaction`, `markets_reaction`.

**Race types:** `senate`, `senate_special`, `house`, `governor`, `lt_governor`, `attorney_general`, `secretary_of_state`, `state_supreme_court`, `state_legislature`, `mayor`, `county`, `ballot_measure`, `other`.

### 11.2 Candidate and entity roster (`monitor ref build-candidates`)
- **FEC OpenFEC API** (free; `FEC_API_KEY` from api.data.gov, `DEMO_KEY` works at low volume): 2026 House and Senate candidates with party, state, district and incumbency. Filter to active, general-election candidates where possible. `VERIFY-LOCAL`: endpoint and filters (`/v1/candidates/?election_year=2026&candidate_status=C…`).
- **Governors and statewide offices:** from Wikipedia pages "2026 United States gubernatorial elections" and per-state pages (parse infobox / candidate tables). Light scraping with polite rate limits is acceptable; Wikipedia's API is preferred.
- **National figures:** a hand list in `config/entities_extra.yaml`. Include the President, Vice President, congressional leaders, party chairs, major governors and prominent surrogates.
- **Aliases:** one Haiku batch call per 100 entities → common short names, nicknames, transliterations (Arabic/Chinese/Russian/Spanish/French renderings of top ~100 names, so non-English mentions resolve).

### 11.3 Race watchlist and "close races" (`config/races.yaml`)
- **Seed at build:** `monitor ref build-races` fetches current public ratings (Wikipedia's aggregated race-ratings tables for 2026 Senate, House and governor races; Sabato's Crystal Ball free pages). Store rating, source and URL. `close_race = rating in {Toss-up, Tilt *, Lean *}`. `VERIFY-LOCAL`: ratings change in the final week, so re-run Tuesday morning.
- **Battleground states:** derived from close Senate/governor races plus an owner override list. A starting point the spec author expects to be relevant (**`VERIFY-LOCAL`, confirm against current ratings**):
  - Senate: GA, MI, NC, ME, NH, OH, TX, IA, AK, NE, MN;
  - Governor: AZ, GA, MI, WI, PA, NV, KS, IA, NH, NY.
- **Redistricting caveat:** several states drew new congressional maps for 2026 (mid-decade redistricting). Census CD boundary files may not match, so **CD-level maps are a stretch item**. House races are shown as lists keyed by `XX-NN`, and county maps are used for local geography.

### 11.4 Poll closings (`config/poll_closings.yaml`)
Per state: first-close and all-close times (ET). Used for the vertical annotations on time charts and the "closing next" header widget. `VERIFY-LOCAL` against a current published poll-closing table before Tuesday. The spec author's approximate expectations:
- 6 pm: IN, KY (partial)
- 7 pm: GA, SC, VA, VT, FL (most), IN/KY (rest)
- 7:30 pm: NC, OH, WV
- 8 pm: AL, CT, DE, DC, IL, ME, MD, MA, MI (most), MS, MO, NH, NJ, OK, PA, RI, TN, TX (most), FL panhandle
- 8:30 pm: AR
- 9 pm: AZ, CO, IA, KS, LA, MN, NE, NM, NY, ND, SD, WI, WY, MI/TX (rest)
- 10 pm: MT, NV, UT
- 11 pm: CA, OR, WA, ID (north)
- Midnight: HI
- 1 am: AK

### 11.5 Geography (`scripts/build_ref_data.py`)
- Census Gazetteer files (states, counties, places) → `ref.*` + `data/ref/`.
- `us-atlas` `states-albers-10m.json` and `counties-albers-10m.json` (pre-projected; no projection math in the browser) → `web/static/topo/`.
- Size check: keep `data/ref` under 150 MB.

---

## 12. Claude API usage

| Purpose | Model | Volume (est.) | Settings |
|---|---|---|---|
| Stage A classify + translate (P2) | `claude-haiku-5-5` | ~40–70k items total, batched 10/call | Lowest reasonable effort; thinking off if the model allows at that effort (`VERIFY-LOCAL` via the claude-api skill); structured output schema; cached system prompt (taxonomy + instructions + few-shots) |
| Stage B deep extraction (P6) | `claude-haiku-5-5` | ~10–20k relevant items | Low effort; structured output |
| Storyline naming, entity resolution, aliases | `claude-haiku-5-5` | ~1–3k calls | Structured output |
| Evening briefs (P12) | `claude-sonnet-5-5` | ~40–60 calls | Medium effort; output must cite supplied item ids |
| Optional "Ask the data" (stretch) | `claude-sonnet-5-5` | interactive | Text-to-SQL over `gold.*` only, read-only connection, show the SQL |

- **Pricing context** (from the skill's cached table; `VERIFY-LOCAL`): Haiku 5.5 ≈ $0.10 / M input, $0.50 / M output; Sonnet 5.5 ≈ $2 / $10.
- **Estimated total for Nov 1–4:** ~$15–35.
- **Budget guard:** `settings.llm.daily_usd_cap = 40`, `hard_cap = 75`.
  - At 80% of the daily cap: Stage A switches to keyword fallback for low-signal sources (scope=international non-politics feeds).
  - At the hard cap: the LLM stops except briefs. The dashboard shows cost and status.
- **Prompt caching:** put stable instructions + taxonomy + few-shot examples + the condensed watchlist in the system prompt. Make it long enough to exceed the minimum cacheable prefix. Check `usage.cache_read_input_tokens` > 0 in the soak test.
- **Robustness:**
  - Retry 429/5xx/connection errors with exponential backoff (SDK built-in plus our own wrapper).
  - Validate every response against Pydantic schemas. On a validation failure, retry once, then mark the item as an error.
  - Check `stop_reason` (handle `refusal` / `max_tokens`).
  - Concurrency semaphores per purpose: A = 6, B = 8, misc = 2.
- **Do not send** `temperature`/`top_p` (removed on current models; `VERIFY-LOCAL`).
- **Prompt rules:**
  - Neutral, non-partisan instructions.
  - "Extract only what the text states; don't infer outcomes."
  - Race calls require explicit language ("projected", "declared the winner", "conceded").
  - Tone toward an entity = how the *article portrays* them (−2 very negative … +2 very positive), *not* whether the news is good for them, with a ≤ 25-word evidence quote copied verbatim.
  - Translations: faithful, concise.

**Pydantic schema sketches** (`llm/schemas.py`):
```python
class ItemA(BaseModel):
    item_ref: str                       # echo of the provided id
    election_relevant: bool; relevance_conf: float  # 0..1
    us_politics: bool
    levels: list[Literal["national","state","local"]]; primary_level: Literal["national","state","local","none"]
    states: list[str]                   # USPS codes
    race_types: list[str]; topics: list[str]  # from taxonomy
    is_results_report: bool
    headline_en: str | None; summary_en: str | None   # only when source language != en

class EntityB(BaseModel):
    name: str; surface_forms: list[str]
    ent_type: Literal["PERSON","ORG","PARTY","OFFICE","MEASURE","GPE","EVENT"]
    role: Literal["candidate","official","election_official","party","org","measure","other"]
    party: str | None; state: str | None; office: str | None; district: str | None
    tone: float | None; tone_evidence: str | None

class RaceCallB(BaseModel):
    race_desc: str; state: str | None; office: str | None; district: str | None
    winner_name: str | None; party: str | None
    called_by: str | None
    status: Literal["projected","declared","conceded","leading","too_close","uncalled"]
    claimed_at_text: str | None; evidence: str

class ItemB(BaseModel):
    summary_en: str; article_tone: float
    entities: list[EntityB]
    places: list[dict]  # {name, place_type, state, district}
    races: list[dict]   # {state, office, district, measure}
    race_calls: list[RaceCallB]
    topics_refined: list[str]
```

---

## 13. Analytics definitions (shown on the Methods page)

- **Volume:** unique relevant items per bin (toggle: all copies).
- **Velocity / "rising":** for entity or storyline *k*, let observed *o* = count in the last 15 min and expected *e* = (count in the trailing 120 min before that) × (15/120).
  - Score = (o − e) / √(e + 1).
  - "Rising" if score ≥ 2 and o ≥ 3.
  - "New" if the storyline's `first_seen_at` was within 30 min.
- **State attention index:** the state's share of relevant items in the window ÷ its share over the baseline (Nov 1 12:00 → Nov 3 12:00 ET). Shrink toward 1 with +5 pseudo-counts. Display as a ratio (1.0 = normal).
- **Tone aggregates:** mean of item-level or mention-level tone with a 95% CI (SE-based).
  - Hidden when n < 5; flagged "low n" when n < 15.
  - Bayesian shrinkage toward 0 with k = 5 pseudo-observations.
  - Always shown with n.
- **Coverage by lean:** for storyline *s* and lean bucket *b*: items(s, b) / items(all relevant, b) in the window. This is the share of bucket *b*'s election coverage devoted to *s*, so a bucket with more outlets doesn't dominate.
- **First to report:** the earliest `first_seen_at` per storyline among canonical items. Caveat: poll timing ±3 min.
- **Race calls in news:** grouped by `race_key` + `winner`. Show the number of distinct outlets reporting, the earliest report, and `called_by` attributions. Never aggregate into a seat count unless the owner enables the stretch item, which carries a disclaimer banner.

---

## 14. Dashboard

### 14.1 Principles
- **Hierarchy:** "what's happening now" (top) → "where" → "what themes" → "who" → "how outlets cover it". Same order every time; a predictable scan path for a room of viewers.
- **One time axis** across time-series panels: shared domain, a shared hover rule (crosshair synced via a small JS event bus), and poll-closing annotations.
- **Every number is clickable** and opens its evidence (the stories behind it). Every LLM-written sentence has footnotes.
- **Global controls in the URL** (query params), so views are bookmarkable and shareable in class.
- **Calm motion:** new items fade in at the top of lists; charts transition at ≤ 300 ms. No layout jumps; panels have fixed heights.
- **Color rules:**
  - **Party** colors only for party: Dem blue, Rep red, Ind/other neutral violet-gray.
  - **Tone** uses a colorblind-safe diverging orange↔purple (ColorBrewer PuOr; orange = negative).
  - **Volume** uses a perceptual sequential scale (viridis or single-hue).
  - **Outlet lean** is shown as a monochrome 5-position glyph (`●○○○○` … `○○○○●`) plus a text label (L / LL / C / LR / R), never as color, so it can't be confused with party.
  - Charts are checked in light and dark themes.
- **Type:** Inter (vendored), tabular numerals for counts.
  - Base 16 px; projector mode 20 px.
  - Chart labels ≥ 13 px (≥ 16 px projector).

### 14.2 Global header (sticky)
- **Left:** title; live clock (ET); LIVE / REPLAY state pill.
- **Center:** KPI tiles:
  - relevant stories per min, with a 2 h sparkline;
  - active storylines;
  - healthy feeds (x / y);
  - "Next polls close: 8:00 pm, PA MI NJ …".
- **Right controls:**
  - **Window:** 15 m | 1 h | 3 h | Since 4 pm | 24 h.
  - **Focus:** All | Battlegrounds | Close races | Watchlist | State ▾.
  - **Level:** All / National / State / Local.
  - **Languages:** EN only / All.
  - **Unique / All copies.**
  - Projector toggle; theme toggle.
- **Status strip** (thin, under the header): pipeline lag (seconds since last commit), LLM status and spend today, disk free, and warnings.

**Focus semantics:**
- `Battlegrounds` = items whose states ∩ battleground set, or whose races ∩ close races.
- `Close races` = races ∩ `close_race`.
- `Watchlist` = races or states with `watch=true` (editable on `/watchlist`, stored in `ui.sqlite`).
- `State` = one state.
- Focus applies to every panel. The map keeps all states visible but dims non-focus states.

### 14.3 Main page `/` — grid at 1920×1080 (12 columns)

**Band 1, "Now"** (top, ~38% height)
- **Live Wire** (4 cols): reverse-chronological relevant items.
  - Each row: time, outlet name + lean glyph, language badge, level badge (N/S/L), state chips, headline (English), and a tone dot when available.
  - Click → article drawer.
  - Prepends via SSE; pauses on hover.
- **Rising Storylines** (5 cols): cards ranked by velocity score.
  - Each card: label; 2 h sparkline (inline SVG, server-rendered); velocity badge; # outlets; coverage-by-lean mini bar (5 segments, monochrome intensities); state chips; "NEW" tag; 1-line summary with footnotes.
  - Click → storyline page.
- **Briefing** (3 cols): latest Sonnet brief with footnotes. Prev/next through earlier briefs. Timestamp + "covers 7:40–8:00 pm". Hover a footnote → headline tooltip; click → article.

**Band 2, "Where"**
- **US Map** (7 cols): D3 choropleth of states.
  - Metric toggle: Attention index (default) | Volume | Mean tone.
  - Battleground states outlined. Hover tooltip: state, metric, n, top storyline.
  - Click → state page.
  - Small inset "states rising" bar list (top 8 by attention change, 15 m vs 2 h).
- **Race Calls in the News** (5 cols): table of `race_key`, winner (party color chip), status, `called_by`, # outlets, first reported.
  - Each row expands to its citations.
  - Banner: "Claims as reported by news outlets; not official results."
  - Filter respects Focus.

**Band 3, "What"**
- **Storyline Stream** (8 cols): Vega-Lite stacked area, centered offset (streamgraph).
  - 5-min bins over the window; top 10 storylines + "Other".
  - Poll-closing rules as labeled vertical lines.
  - Hover → highlights the storyline in other panels. Click → storyline page.
  - Toggle: streamgraph ↔ normalized 100% stack.
- **Topic Heat** (4 cols): heatmap of topics × 15-min bins (last 3 h), sequential scale. Click a cell → evidence list.

**Band 4, "Who"**
- **Entity Rank Bump Chart** (6 cols): top 12 entities by mentions per 15-min bin; lines colored by party; label at the line end. Click → entity page.
- **Tone by Outlet Lean** (6 cols): for the top 10 entities, dot plot of mean tone with CI per lean bucket (L, LL, C, LR, R), plus overall. Low-n points are hollow, and n is in the tooltip.

**Band 5, "Coverage"**
- **Coverage-by-Lean Matrix** (7 cols): rows = top 12 storylines; columns = lean buckets + Non-US; cell = coverage share (§13). Sorted by divergence (max − min across buckets). Click a cell → evidence.
- **Global Press** (5 cols): non-English coverage share by language over time (small multiples), plus the latest 6 translated headlines (original-language tooltip).

**Stretch panels:**
- Co-mention network (D3 force).
- First-to-report timeline (beeswarm by outlet per storyline).
- Source health table on the main page (it is on `/status` anyway).

### 14.4 Drill-down pages (full page; also openable as a right-side drawer for articles)
- **`/story/{storyline_id}`**:
  - header: label, summary with footnotes, first seen, totals;
  - volume over time (by level);
  - outlets list with lean glyphs and first-report times;
  - entities (bar, by mentions), with tone;
  - state map mini (states involved);
  - coverage-by-lean bars;
  - article list (filterable, with dup groups collapsed);
  - "related storylines" (centroid nearest neighbors).
- **`/entity/{entity_id}`**:
  - profile (party, office, race, incumbent);
  - mentions over time;
  - tone over time with CI band;
  - tone by outlet lean;
  - co-mentioned entities;
  - storylines involved;
  - evidence quotes (tone extremes, both directions, with citations);
  - race-call claims involving them.
- **`/state/{usps}`**:
  - races in the state (with ratings and watch toggle);
  - attention index over time;
  - county choropleth (volume);
  - local outlets and their latest items;
  - state storylines;
  - race-call claims;
  - poll-closing time.
- **`/article/{item_id}`** (drawer + page):
  - headline (original + English);
  - outlet, lean glyph and a link to the rating citation;
  - times (published / first seen);
  - extracted text, with entity spans highlighted (colored underline by type; hover → entity card) and place spans linked;
  - per-entity tone with evidence quotes;
  - classification badges (levels, topics, race types);
  - storyline membership;
  - 8 nearest-neighbor items (embedding);
  - syndication group;
  - "Open original" link;
  - raw parser outputs (collapsible JSON, for teaching).
- **`/search`**: semantic search. The query is embedded with the `query: ` prefix and cosine-matched against all embeddings (brute force is fine at < 200k vectors). Optional keyword filter, plus facets (level, state, outlet, lean, time).
- **`/sources`** (reference page):
  - sortable table: outlet, scope, state, country, language, lean label + **citation link and retrieval date**, Ad Fontes values where available, ownership, paywall, feeds, fetch health (last success, error rate), items collected, relevant share;
  - the inclusion / exclusion criteria text (§5.3);
  - a lean-distribution chart.
- **`/methods`**: renders `docs/methods.md`. Covers pipeline diagram, models and versions, prompts (abridged), metric definitions (§13), known limitations (paywalls, polling latency, LLM error rates, tone caveats, redistricting caveat), and the cost ledger summary.
- **`/status`**: feed health grid, stage lags, queue depths, LLM spend by purpose, disk breakdown (`monitor disk` data), last maintenance, last backup.
- **`/watchlist`**: edit watched races and states; battleground override.

### 14.5 Citations and footnotes (system-wide)
- **Footnote component:** `[n]` superscript.
  - Hover → popover: headline, outlet + lean glyph, time.
  - Click → article drawer.
  - Every brief and storyline summary stores `citations JSON {n: item_id}`.
- **Evidence drawer:** any aggregate (map state, heat cell, bump point, matrix cell, KPI) opens a drawer listing the underlying items (paged, sorted by time), with the exact filter shown ("GA · 8:00–8:15 pm · unique · focus=Battlegrounds").
- **Lean labels** always link to the `/sources` row, and from there to the external rating page.
- **Validator:** LLM text with a sentence lacking a citation is trimmed. Citations pointing to item ids that weren't supplied are dropped. Log the counts.

### 14.6 Projector mode and tour
- **Projector mode** (`p` key or toggle):
  - 1.25× type scale and thicker strokes;
  - high-contrast light theme by default (dark optional);
  - hides the status strip and secondary controls;
  - reduces the main page to Bands 1–3, with Bands 4–5 reachable by scrolling or keys;
  - minimum chart label 16 px.
- **Tour mode** (`t` key): full-screen rotation every 30 s (configurable) through Rising Storylines → Briefing → Map → Stream → Who → Race Calls. Pauses on any interaction. Shows a progress bar.
- **Keyboard:**
  - `1`–`5` jump to bands; `f` cycles Focus; `w` cycles Window; `/` search; `Esc` closes the drawer; `←`/`→` previous/next brief;
  - `r` toggles Replay with a speed control (1×, 5×, 20×) and a time scrubber.
- **Responsive breakpoints:** ≥ 2200 px (more rows visible, larger charts); 1600–2199 (default); 1280–1599 (bands stack to 2 columns, matrix scrolls internally). Not supported below 1280.

### 14.7 Real-time update mechanics
- **SSE endpoint** `/events` (sse-starlette). Events:
  - `snapshot` (new commit: `snapshot_id`, `changed_tables`);
  - `wire` (rendered HTML rows for new relevant items since the last event);
  - `brief` (new brief id);
  - `status` (every 10 s).
- **HTMX:** panels use `hx-get="/frag/<panel>?{global params}" hx-trigger="load, sse:snapshot throttle:10s"` (`VERIFY-LOCAL`: htmx 2 SSE extension syntax). Wire rows use `sse-swap="wire" hx-swap="afterbegin"`.
- **Vega:**
  - Chart specs come from `/api/spec/<chart>` (built once with Altair and cached).
  - Data comes from `/api/data/<chart>?…` as JSON rows.
  - On `snapshot`, JS refetches the data and calls `view.data('table', rows).run()`. **No re-embed** on every update (prevents flicker and keeps hover state).
- **D3 map:** keyed joins; transitions on fill only.
- **Server caching:** the query result cache is keyed by `(snapshot_id, query_name, params)`, LRU of 512 entries. Target p95 < 150 ms per fragment at 100k items.
- **REPLAY:** `as_of` advances on a client-driven clock. Panels re-query with `as_of`, and SSE is ignored while in replay.

---

## 15. CLI (`monitor …`, Typer)

```
monitor init                      # create dirs, attach lake, create schemas/tables, set options
monitor verify                    # run the VERIFY-LOCAL checks that are scriptable (versions, attach RO, option names, API key, model ids, fastembed model)
monitor sources discover          # §5.2 → config/sources.csv + reports/feed_validation.md
monitor sources check             # fetch-test all enabled feeds once
monitor sources export-opml|import-opml PATH
monitor ref build-geo | build-candidates | build-races | build-aliases | all
monitor pipeline [--no-llm] [--stages a,b,…]   # run the writer process
monitor web [--host] [--port] [--lan]
monitor rollups --rebuild
monitor brief --now [--focus battleground]
monitor disk
monitor prune …                   # §8.4
monitor backup --now
monitor replay-check --from T --to T  # sanity report of counts by bin (for demo prep)
monitor cost                      # LLM spend by purpose/model/day
```

---

## 16. Disk budget (target ≤ 8 GB; recommend ≥ 15 GB free before starting)

| Item | Estimate |
|---|---|
| Python venv (no torch: fastembed/onnxruntime, spaCy small) | 1.0–1.5 GB (+1.5 GB if torch is needed) |
| Models (e5-small ONNX, spaCy sm ×2, lang-ID) | 0.3–0.6 GB |
| Reference data (gazetteers, topojson, roster) | ≤ 0.15 GB |
| Lake data (≈ 60k items, ≈ 15k full texts, embeddings, NLP outputs) | 0.4–0.8 GB (zstd) |
| Snapshot-retained files (compaction churn) | 0.3–1.0 GB |
| Catalog + backups | 0.1–0.5 GB |
| Logs (rotate at 50 MB × 5) | ≤ 0.25 GB |

Steps for Claude Code at Phase 0:
1. Run `df -h ~` and `du -sh ~/Library/Caches ~/.cache 2>/dev/null` and report.
2. Point `HF_HOME` and the fastembed cache at `data/models/`.
3. If free space is < 15 GB, suggest cleanup candidates to the owner (don't delete anything yourself).

---

## 17. Operations

### 17.1 Run-of-show for Nov 3 (`docs/run_of_show.md`)
- **12:00 pm:** `monitor ref build-races` (refresh ratings); `monitor sources check`; review `/status`.
- **12:30 pm:** replay test of the last 6 h at 20×. Verify every panel renders at 1280×720 and at 1920×1080.
- **2:30 pm:** restart both processes clean; `monitor backup --now`; confirm the LLM cap; plug into the projector and check projector mode.
- **3:00 pm:** freeze. From here only config edits (sources enable/disable, thresholds, watchlist).
- **6:00 pm → 1:00 am:** watch the status strip. If lag exceeds 3 min, disable tier-3 feeds, then pause Stage B for international sources.
- **After 2:00 am:** reduce polling to tier 2/3; `monitor prune --dry-run` to review.

### 17.2 Degradation ladder (automatic where noted)
1. **LLM 429s or outage:** retries with backoff. After 2 min, Stage A switches to keywords (automatic), and Stage B queues and catches up later. The UI shows "LLM degraded".
2. **CPU thermal pressure:** pipeline lag > 120 s → halve the embed batch rate and pause backfills (automatic).
3. **Disk:** the guard in §8.4 (automatic).
4. **Feed failures:** circuit-break a feed after 5 consecutive failures (retry every 15 min). Shown on `/status`.
5. **Web slow:** increase fragment throttle to 20 s; projector mode reduces panels.

### 17.3 Ethics and etiquette
- Identify the poller with a descriptive User-Agent including a contact email (owner's choice, set in `settings.toml`).
- Honor robots.txt for article fetches, use conditional GET, and rate-limit per domain.
- Store full text for personal and classroom analysis only. Don't republish full texts. The dashboard shows extracts and always links to the original.

---

## 18. Testing

- **Unit:** URL canonicalization, dedupe, velocity math, attention index, tone aggregation with shrinkage, footnote validator, race-key normalization, gazetteer disambiguation.
- **Fixtures:** `tests/fixtures/feeds/*.xml` (RSS 2.0, Atom, malformed); 30 hand-labeled articles across levels and languages (`tests/fixtures/golden.jsonl`).
- **LLM contract tests:** run P2/P6 on the golden set. Assert schema validity and agreement thresholds (relevance accuracy ≥ 90%, primary_level ≥ 80%). These cost cents; run them on prompt changes.
- **Soak test:** two-process DuckLake read/write for 10 min (§7).
- **Load test:** synthetic burst of 2,000 items in 5 min through the pipeline with `--no-llm` (keyword path). Check lag and dashboard latency.
- **Visual check:** Playwright screenshots of `/`, `/story/x`, `/state/GA` at 1280×720, 1920×1080 and 2560×1600, in light, dark and projector modes. Saved to `reports/screens/` for owner review.

---

## 19. Build phases and acceptance criteria

### Phase 0: Foundations and baseline (Sun evening, ~3–4 h). **Top priority: start collecting.**
- [ ] Disk check (§16). Scaffold the repo (§6) with uv. `CLAUDE.md` from §0.
- [ ] `monitor verify` script: DuckDB + ducklake + sqlite extension versions; attach a read-write and a read-only connection; set compression and inlining options; Anthropic key works with `claude-haiku-5-5`; fastembed model availability. Write results to `docs/verify_log.md`.
- [ ] `monitor init` creates all tables (§9).
- [ ] `monitor sources discover` over `sources_seed.csv` → validated `config/sources.csv` + report. **Owner reviews the failures.**
- [ ] Poller running under honcho, writing `raw.fetches` and `raw.items` with 30-second micro-commits. Maintenance and backup jobs scheduled.
- [ ] Two-process soak test passes.
- **Accept when:** ≥ 250 healthy feeds; items accumulating; `monitor disk` works; no tiny-file explosion (< 200 Parquet files after 2 h).

### Phase 1: Core enrichment (Mon morning)
- [ ] P1 lang, P2 classify_a (with keyword fallback), P3 embed, P4 dedupe, P9 storylines (with Haiku naming), P11 rollups (bins, storyline, state, source, topic).
- [ ] Backfill Phase 0 items. LLM ledger and budget guard working.
- [ ] Ref data: geo, states, poll closings, races (initial), candidate roster v1.
- [ ] Golden-set contract tests pass.
- **Accept when:** median first-seen → classified ≤ 60 s; storylines look coherent on the baseline (spot-check 20); `cache_read_input_tokens` > 0.

### Phase 2: Dashboard core (Mon afternoon/evening)
- [ ] App shell, header, status strip, global controls in the URL, Focus semantics.
- [ ] Band 1 (Live Wire, Rising Storylines; Briefing placeholder), Band 2 (Map; Race Calls table can be empty), Band 3 (Stream, Topic Heat).
- [ ] SSE + HTMX + `view.data()` updates; `as_of` replay mode.
- [ ] Projector mode + keyboard shortcuts.
- **Accept when:** live updates with no flicker; p95 fragment < 150 ms; Playwright screenshots at 3 sizes reviewed by the owner.

### Phase 3: Depth and trust (Tue morning)
- [ ] P5 fetch_text, P6 extract_b (entities, tone, places, race calls), P7 entity resolution, P8 geo.
- [ ] Band 4 (Bump, Tone by Lean), Band 5 (Coverage Matrix, Global Press).
- [ ] Drill-down pages: article, storyline, entity, state; search.
- [ ] Footnotes and evidence drawers everywhere; `/sources` with lean citations; `/methods`; `/status`; `/watchlist`.
- [ ] P12 briefs (Sonnet) with the citation validator.
- [ ] Tour mode.
- **Accept when:** every aggregate opens its evidence; every brief sentence is cited; lean labels have citations or show "Unrated".

### Phase 4: Hardening (Tue 12:00–3:00 pm)
- [ ] Load test; degradation ladder drills (kill the API key → keyword fallback; fill a temp disk quota → guard).
- [ ] Refresh races and ratings; run-of-show rehearsal; freeze at 3:00 pm.

### Stretch (only if ahead, or after the election)
Co-mention network · first-to-report beeswarm · "Ask the data" (Sonnet text-to-SQL, read-only) · CD maps (2026 lines) · live-blog tracking (re-fetch live-blog URLs every 2 min and diff new posts) · GDELT 15-min supplement · seat-count-from-news-calls widget (disclaimered) · post-election "replay the night" export for class.

---

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| DuckLake concurrency or option-name surprises | Phase 0 verify + soak test; Plan B Postgres catalog; Plan C Parquet export of gold |
| Not enough time | Strict phase order; stretch list; Phase 2 alone is a usable dashboard |
| Feeds missing or wire copy absent | Validate early; section feeds; dedupe syndication; Spanish-language US outlets; nonprofit state networks |
| Paywalls limit full text | Stage B falls back to the summary; metrics don't depend on full text |
| LLM errors or bias in tone | Neutral prompts; evidence quotes visible; n and CI always shown; Methods page caveats; golden-set checks |
| Misreported race calls | Labeled "claims in news"; requires explicit language; shows # outlets and attributions; no seat totals by default |
| Thermal throttling (fanless Air) | Small local models, batching, API-first NLP, auto-throttle |
| Disk exhaustion | Guard + prune CLI + no torch by default + log rotation |
| Projector readability | Projector mode, high-contrast theme, Playwright screenshot review at 1280×720 |
| Catalog corruption | 15-min rotating backups; never edit the catalog manually |

---

## 21. Open questions for the owner (defaults in **bold**, used if no answer)

1. **Q1, User-Agent contact:** the email to put in the poller's User-Agent string? (**default: none, generic UA with project name**)
2. **Q2, Exposure:** localhost only, or LAN-visible so a second device can view? (**localhost**)
3. **Q3, State-affiliated outlets:** include outlets like Xinhua, CGTN or TASS behind an "off by default" toggle, labeled state-controlled, for contrast in the Global Press panel? (**exclude**)
4. **Q4, Additional languages** beyond the UN six (e.g. Portuguese, German, Japanese, Hindi)? (**no**)
5. **Q5, Opinion pieces:** include them (tagged `opinion` via URL/section heuristics and the LLM), or exclude them from metrics? (**include, tagged, excluded from tone-by-lean by default**)
6. **Q6, Brief cadence on election night:** every 20 min, or tied to poll-closing times (just after each close)? (**every 20 min + an extra brief 10 min after each major close**)
7. **Q7, LLM budget caps:** daily $40 / hard $75 OK? (**yes**)
8. **Q8, Classroom use after Nov 3:** a polished "replay the night" mode, a static export, or both? (**replay mode only**)

---

## Appendix A: `settings.toml` skeleton

```toml
[paths]
catalog = "data/catalog.sqlite"
data = "data/lake/"
models = "data/models/"
ui_db = "data/ui.sqlite"

[poll]
tier_seconds = [60, 180, 600]
per_domain_concurrency = 2
jitter_pct = 15
user_agent = "ElectionNightMonitor/1.0 (personal research; contact: <owner>)"

[commit]
batch_seconds = 30
maintenance_minutes = 15
backup_minutes = 15

[nlp]
relevance_threshold = 0.5
embed_model = "intfloat/multilingual-e5-small"
storyline_join = 0.82
storyline_merge = 0.88
storyline_promote_items = 3
storyline_promote_sources = 2

[llm]
model_bulk = "claude-haiku-5-5"
model_brief = "claude-sonnet-5-5"
concurrency_a = 6
concurrency_b = 8
batch_a = 10
daily_usd_cap = 40
hard_usd_cap = 75
brief_minutes = 20

[web]
fragment_throttle_seconds = 10
default_window = "1h"
timezone = "America/New_York"

[disk]
warn_gb = 10
degrade_gb = 5
autoprune_gb = 3
```

## Appendix B: Local verification checklist (`docs/verify_log.md` template)

| # | Check | How | Result |
|---|---|---|---|
| V1 | DuckDB + ducklake + sqlite ext versions | `uv run python -c "import duckdb; print(duckdb.__version__)"`; `INSTALL ducklake; LOAD ducklake;` | |
| V2 | Read-only attach syntax works while a writer is attached | 2-process soak script | |
| V3 | Option names: compression, compression level, target file size, data inlining | DuckLake docs (`ducklake.select/docs`) + `SELECT * FROM lake.options()` or equivalent | |
| V4 | Maintenance function names (merge, flush inlined, expire, cleanup) | Docs + dry run on a scratch lake | |
| V5 | Snapshot listing function (for change notification) | Docs | |
| V6 | fastembed supports multilingual-e5-small (else choose an alternative) | `fastembed` model list | |
| V7 | Anthropic: model ids, structured-output call shape, effort/thinking settings for Haiku 5.5 & Sonnet 5.5, prompt-caching minimum prefix | `claude-api` skill + a 1-call smoke test | |
| V8 | htmx 2 SSE extension attribute syntax | htmx docs | |
| V9 | Vendored asset versions (htmx, vega, vega-lite, vega-embed, d3, topojson-client, us-atlas, Tailwind standalone) | `scripts/vendor_assets.sh` pins exact versions | |
| V10 | OpenFEC endpoint and filters for 2026 candidates | Test call with DEMO_KEY | |
| V11 | Race ratings sources reachable; parse OK | `monitor ref build-races --dry-run` | |
| V12 | Poll-closing times confirmed | Compare `poll_closings.yaml` with a current published table | |
| V13 | Lean ratings for enabled outlets confirmed with citations | Owner review of `lean_ratings.csv` | |
| V14 | Battleground/close-race list confirmed | Owner review of `races.yaml` | |
