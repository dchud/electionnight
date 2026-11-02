# Election Night News Monitor

`SPEC.md` is the source of truth for what gets built. Plan and status live in
the beads tracker (`br`), under the MVP epic.

## Instructions for Claude Code (read first)

1. **Copy this section into the repo's `CLAUDE.md`** at scaffold time. Keep
   `SPEC.md` in the repo root as the source of truth.
2. **Work in the phase order in §19.** Each phase has acceptance criteria.
   Don't start a later phase until the earlier one's criteria pass, unless the
   owner says so. Collecting a baseline early matters more than polish.
3. **Items tagged `VERIFY-LOCAL`** are facts the spec author couldn't confirm:
   library APIs, option names, feed URLs, election facts. Before you rely on
   one, check it (docs, `pip show`, a small test script, or a web search) and
   record the result in `docs/verify_log.md`. If reality differs from the spec,
   follow reality and note the difference.
4. **Single-writer rule.** Only the `pipeline` process writes to the DuckLake.
   The `web` process never writes to the lake (its own UI-preferences DB is
   fine).
5. **Append-only by default.** Never UPDATE or DELETE rows in lake tables
   during the event. Corrections go in as new rows with a newer `created_at`.
   Readers take the latest row. This keeps snapshots cheap and replay correct.
6. **Respect the disk budget (§16).** Check free space before downloading
   models or data. If free disk drops below 8 GB, stop and tell the owner.
7. **Prefer boring and robust.** Deadline beats features. When a stretch item
   threatens a core item, drop the stretch item and say so.
8. **Claude API:** use the official `anthropic` Python SDK. Before writing API
   code, consult the `claude-api` skill (or the live docs) for current request
   shapes: structured outputs, thinking/effort settings, prompt caching. Don't
   guess parameters. Keep model IDs in config.
9. **Never commit secrets.** `ANTHROPIC_API_KEY` and optional `FEC_API_KEY`
   come from the environment or `.env`, which is git-ignored.
10. **Vendor every frontend asset locally** (no CDNs at runtime). The classroom
    network may be unreliable.

## Commands

```bash
uv sync                     # install
scripts/check.sh            # format, lint, types, tests, process-label lint
uv run pytest -m llm        # golden-set contract tests; costs cents
uv run pytest -m slow       # soak and load tests
uv run monitor --help       # every operating command
caffeinate -is uv run honcho start pipeline web   # run the system
```

## Key files

| Path | What it holds |
|---|---|
| `SPEC.md` | Requirements, data model, dashboard behavior |
| `.env.example` | Every setting and its default |
| `src/monitor/settings.py` | Settings model |
| `src/monitor/lake.py` | Lake connections, table definitions, writer |
| `src/monitor/pipeline.py` | The writer process |
| `src/monitor/parsers/` | One module per parser |
| `src/monitor/web/` | Dashboard |
| `config/` | Sources, ratings, taxonomy, races, poll closings |
| `docs/verify_log.md` | Checked facts and where the spec differs |

## Configuration

Tunables are environment variables in `.env`, using `__` for nested groups
(`LLM__DAILY_USD_CAP=40`). Structured data stays in `config/*.csv` and
`config/*.yaml`. Add a new setting to `settings.py` and `.env.example`
together; a test fails if they disagree.

## Operating rules

- The `pipeline` process holds `data/writer.lock`. A CLI command that writes
  sends its request through `data/control/` while the pipeline runs.
- Tests use a scratch lake under `tmp/`. Never point a test at `data/`.
- Pipeline code changes take effect after a pipeline restart.
- Outbound requests never carry the owner's email address, in any header or
  body. Subagent briefs state this rule and relay the owner's decisions.
- Never send email or attempt to, for any reason, through any tool or code.

## Conventions

**Tone.** Flat, direct, literal, in chat and in everything committed. No
enthusiasm, marketing language, idioms, or euphemisms. Name exactly what a
change delivers. Documentation states what to do and what is true; it does not
argue with an imagined objector.

**Workflow.** Everything lands through a PR. Commit and open the PR without
asking when local checks pass. Report the PR link and CI result, then stop.
Never merge. Never delete remote branches. Never run registry or publish
commands. Stage by path, never `git add -A`, and check
`git diff --cached --stat` before committing.

**Commits and PRs.** Conventional-commit subjects, bodies wrapped at 72. No
Claude attribution trailers or footers. PR titles carry no ticket IDs. PR
bodies start from `.github/PULL_REQUEST_TEMPLATE.md` and end with
`Bead: bd-XXXX`.

**Persistent artifacts.** Source, tests, and docs carry no phase or step
numbers, PR or ticket identifiers, version-tagged claims, or wording about
project state over time ("currently", "not yet", "planned"). `scripts/check.sh`
enforces the high-confidence shapes. Commit messages, PR bodies, and tickets
may carry ticket IDs.

**Markdown wrapping.** Repository Markdown wraps at 79 columns. Text submitted
through GitHub or `br` is unwrapped. Code blocks, tables, and long URLs never
wrap.

**Tracker.** `br` holds the plan. Never pass `--slug`. Never use `br edit`.
Titles are descriptive, with no phase or priority text. `br update
--description` replaces, so run `br show` first. Close a ticket in the commit
that finishes it, flush with `br sync --flush-only`, and commit
`.beads/issues.jsonl`.

**Tools.** `uv` for every Python invocation. Line length 79. `rg` over `grep`,
`fd` over `find`, `dust` over `du`.

**Scratch.** All scratch goes in the git-ignored `./tmp/`. Never write to
`/tmp`, `/private/tmp`, `/var/folders`, or the filesystem root. Subagents
follow the same rule.

**Verification.** Run the check and read its output before saying anything is
done. Report failures and skipped steps as they are. Check a subagent's
findings before acting on them.

**Design.** There is no installed base. Choose the cleanest end state, judged
on internal consistency and on whether the rule fits in one sentence.
