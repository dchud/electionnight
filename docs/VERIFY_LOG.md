# Verification log

Facts the spec marked for local checking, and where the system differs from
the spec's text. `monitor verify` rewrites rows V1 through V7.

| # | Check | Result |
|---|---|---|
| V1 | DuckDB, ducklake, and sqlite extension versions | pass 2026-11-01: duckdb 2.0.0, ducklake 417a9117f5, sqlite_scanner e223dc0149, DuckLake format 1.1 |
| V2 | Read-only attach works and rejects writes | pass 2026-11-01: `READ_ONLY` attach reads and rejects INSERT |
| V3 | Lake option names accepted | pass 2026-11-01: data_inlining_row_limit, parquet_compression, parquet_compression_level, target_file_size |
| V4 | Maintenance function names | pass 2026-11-01: ducklake_flush_inlined_data; ducklake_merge_adjacent_files; ducklake_expire_snapshots; ducklake_cleanup_old_files |
| V5 | Snapshot listing function | pass 2026-11-01: `lake.snapshots()` columns: ['snapshot_id', 'snapshot_time', 'schema_version', 'changes', 'author', 'commit_message', 'commit_extra_info'] |
| V6 | Embedding model available through fastembed | pass 2026-11-01: intfloat/multilingual-e5-small, custom ONNX export, returns 384 dimensions |
| V7 | Anthropic key, model ids, request settings, prompt caching | pass 2026-11-01: claude-haiku-5-5 parse with effort low and thinking disabled OK, cache read 2906 tokens on the second call; claude-sonnet-5-5 adaptive at medium OK; SDK 2.3.0 |
| V8 | htmx 2 SSE extension attribute syntax | |
| V9 | Vendored asset versions | |
| V10 | OpenFEC endpoint and filters for 2026 candidates | |
| V11 | Race ratings sources reachable and parsed | |
| V12 | Poll-closing times confirmed | |
| V13 | Lean ratings confirmed with citations | |
| V14 | Battleground and close-race list confirmed | |

## Differences from the spec

- **Vectors.** DuckLake rejects `FLOAT[384]` with "unsupported type". Tables
  store `FLOAT[]`; queries cast to `FLOAT[384]` for array functions.
- **Embedding model.** fastembed does not list
  `intfloat/multilingual-e5-small`. The model's Hugging Face repository has
  `onnx/model.onnx`, registered with `TextEmbedding.add_custom_model`. Measured
  286 items per second on the CPU, with no torch install.
- **Similarity thresholds.** With this model an unrelated English pair scored
  0.80 and a same-story English and Spanish pair scored 0.93. See
  `reports/similarity_histogram.md` for the live distribution.
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
