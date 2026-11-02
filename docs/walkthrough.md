# Class walkthrough

A script for teaching from the under-the-hood views on election night. All
times are Eastern. The dashboard is the main view; these pages explain it.

## Setup

1. Open <http://127.0.0.1:8000> and scroll past the last band. The footer link
   "Under the hood" opens `/under`.
2. Press `p` for projector mode: larger text, one concept per screen, no
   horizontal scrolling.
3. Move between sections with `j` and `k`. Press `r` to refresh. Pages also
   refresh every 30 seconds unless "Every 30 s" is off; a form with focus
   holds the refresh.
4. Every section that reads a table has a "Show query" disclosure with the SQL
   that ran, where it ran, and how long it took. Open one early, so the class
   knows it is there. Feed state comes from the status file, and JSON schemas
   come from code, so those two have none.

These pages hold no SSE connection, so several tabs cannot use up the browser's
connections to the dashboard. Queries that open data files run in a child
process at low priority, stopped at 10 seconds. The keyword comparison gets 30
seconds, and an archive read gets 15.

## The order

| When | Open | The question it answers | The caveat to raise |
|---|---|---|---|
| Opening | `/under/coverage` | What can this system see, and what can it not? | The lean of most outlets is unknown, and major outlets have no working feed |
| Before polls close | `/methods` | How does an article get from a feed to the screen? | The diagrams show what runs, not whether its answers are right |
| Before polls close | `/under/lake` | What does DuckLake store, and how do we know? | A row count measures writes, and statistics leave out inlined rows |
| Before polls close | `/under/inventory` | How many records are there, and where did they come from? | Rows are not records, and most early items were backlog |
| Before polls close | `/under/storage` | How is this stored while it is being written? | Merge and cleanup change files, not answers |
| Before polls close | `/under/data-model` | What does one table look like, from code to file? | A schema constrains shape, not truth |
| First results, about 7 pm | `/under/trace` | What happened to this one article? | Older items lack traces, and a trace shows what ran, not whether it is right |
| After a race call appears | `/under/models` | Which model produced this value? | Agreement between two methods is not accuracy |
| Quiet stretch | `/under/feeds` | What does polling look like as a system? | Yield credits only the first feed to serve an item |
| Quiet stretch | `/under/costs` | What did this cost? | Dollar figures are estimates, not an invoice |
| Questions | `/under/queries` | What can this stack do with each kind of data? | Each query says what its result does not mean |
| Questions | `/under/records`, `/under/sql` | Can we check that ourselves? | Stored values can mislead; read the labels |

## What to say

### Coverage

Start here. Everything after depends on it. Read the large number at the top
aloud: the share of enabled outlets with no lean rating. It counts non-US
outlets, which this scale does not rate, and the breakdown sits under it. Then
read "The gaps, by name", and name AP, Reuters, the Washington Post, CNN, and
USA Today.

Ratings come from AllSides search-result text, because allsides.com blocks
direct fetches. The owner confirmed national and analysis outlets only, so no
state or local outlet has a rating.

An enabled outlet has a feed on the list. A working outlet is an enabled outlet
with a feed that succeeded in the last hour, according to the status file. The
page shows both.

Sort out two ideas on the location table. Enabled outlets against target
measures the source list. Story share against seat share measures attention,
and House seats stand in for population because the gazetteer has none. A high
ratio can come from a contested statewide race.

End on "Conclusions this data cannot support". Ask the class to add one.

### Methods

Open `/methods`. The left navigation follows the section in view. Walk through
the three diagrams: feed to screen, the processes and the single-writer rule,
and DuckLake. Point out that `classify_a` feeds two branches, that every row
the pipeline writes goes through one batch writer, and that maintenance runs
flush and merge directly on the same connection. Each section links to its live
view. The table at the end names the setting or constant behind each value, and
a test compares them.

### DuckLake

Open `/under/lake`. Start at the table map: it shows the overall shape, and the
solid lines are values the code keeps equal, because DuckLake has no foreign
keys. The dashed lines show what the rollups read to derive gold tables. The
catalog is plain SQL, so read a row count aloud and open its query. Choose
`nlp.embeddings` in the table browser. `ducklake_column` holds `vec` as a list
with a child `element`, and the statistics sit on the child. On that row, Nulls
counts NULL lists, empty lists, and null elements. This table never inlines, so
choose `raw.items` to compare the rows in Parquet with the inlined rows.
Statistics cover only the first of those. A sample row shows a vector's length
and first values, and article text is cut to an excerpt in the query.

Inventory compares rows with keys for every table. Storage has compression and
time travel, and Data model has the table as the code declares it.

### Inventory

Show `gold.source_bins`: more rows than keys. Ask why. Tables are append-only,
so a recomputed bin is a new row, and readers take the newest row per key.

Show the arrival chart. The spike at the first fetch, 1:14 pm on November 1, is
feed backlog, not news. A second spike is the outlets added later that
afternoon. The dashboard places an item at its published time in two cases: it
was published over 2 hours before first seen, or it was published during a
collection gap, or in the 20 minutes before it, and first seen within 15
minutes after it. `gold.*` bins use `first_seen_at` unchanged. So the two
disagree on purpose. Dashboard panels read an in-memory fact table, not
`gold.*`.

`source_id` is the first feed to serve an item, which favors the fastest
poller.

### Storage

Point out inlined rows against Parquet rows: a commit every 30 seconds would
otherwise create thousands of tiny files. Pick `nlp.embeddings` to show a table
that never inlines. Show column compression: repeated values compress well, and
vectors do not.

Then run a time-travel comparison, with Snapshot A a few hundred before the
newest. A merged file keeps each row's snapshot range, so the old answer stays
exact after the files it came from are deleted.

### Data model

Choose `nlp.embeddings`. The code declares `vec FLOAT[]`, because DuckLake
rejects `FLOAT[384]`. Show how the Parquet footer holds that list. Open a JSON
schema at the bottom, and connect it to the columns in the same table.

### Trace

Open `/under/trace` and choose a recent item. Walk down the page:

1. The raw feed entry, then the original bytes. Open "Original feed bytes".
2. Each stage's row and latency. Buffered is when a stage finished, and
   committed is when a reader could see it. When a stage has several rows, the
   line describes the newest, the one readers use.
3. The prompt, request, and response, with article text cut to an excerpt.
4. Entity and place resolution, with any quote the citation check threw out.
5. Dedupe and clustering. A stored `similarity` of 1.0 is a placeholder for an
   item that started a storyline, and `nlp.cluster_decisions` has the true
   nearest similarity.
6. Which panels count it, using each panel's own filter.

Then trace an item from the first fetch, to show what older items lack. On
those, a `dup_groups.method` of `url` means no match was found; no URL was
compared.

The Trace link also appears on every article page.

### Models

Read the column map from top to bottom. Local tools and model calls sit side by
side. The keyword comparison reruns the fallback classifier over a sample of
items the model classified. It costs nothing, and it shows what the pipeline
would lose without the API. Neither column is ground truth.

### Feeds

Most polls return 304 and carry no body. Entries served far exceed items kept,
because a feed repeats its entries. The saving from a 304 is an estimate from
that feed's mean response size. Breakers open after 5 failures in a row, and
that state lives in the pipeline's memory.

### Costs

Compare the estimate with the same calls uncached. The saving comes from a long
system prompt repeated on every call. `usd_est` is computed from configured
prices, not an invoice, and SDK retries are not counted.

### Queries

Use `/under/queries` when the class asks what this stack can do. The first
button groups queries by data shape, and the second by what produced the
values. Start with the query on the latest-row rule, which gives two sums from
one table, and then the English and Spanish vector match. A Spanish item's
vector comes from the English the classifier wrote, so that match rests partly
on the translation. Each query says what its result does not mean, and read
that aloud. Its link opens it in the SQL console without running it.

### Records and SQL

Use `/under/records` for "show me one". It filters stored values and links to
the trace. It is a different tool from `/search`, which ranks by meaning.

Use `/under/sql` for a question the views do not answer. It takes one SELECT or
EXPLAIN, at most 1,000 rows, and stops at 10 seconds. It answers on loopback
only, and it logs each query. These work:

```sql
SELECT count(*) AS n_rows, count(DISTINCT (bin_start, source_id)) AS n_keys
FROM lake.gold.source_bins;

SELECT method, count(*) AS n FROM lake.nlp.dup_groups GROUP BY 1 ORDER BY n
DESC;

SELECT purpose, round(sum(usd_est), 2) AS usd
FROM lake.nlp.llm_calls GROUP BY 1 ORDER BY usd DESC;
```

Do not demonstrate a mutation on the live lake. To show the safeguards, read
the section at the bottom of `/under/sql`, or run the scratch-lake test, which
tries each mutation against a throwaway lake:

```bash
uv run pytest tests/test_under_sql.py --basetemp=tmp/pytest-under
```

## If something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| "These figures were scanned ... ago" | A result is kept for 2 minutes, or 10 minutes for Parquet footers, time travel, capture start, and the query gallery. If a new scan does not finish, the last result stands in | Nothing |
| "This scan did not finish" or "A scan did not finish" | A figure on that page is not known. It does not mean zero | Press Retry |
| "Not finished when the child process stopped" | A scan reached its 10-second limit in its own process. What finished is kept | Press Retry. The next run continues with what is left |
| "Another scan is running" | Scans run one at a time, and a page does not wait for one | Press Retry in a moment |
| "Status unavailable" | The status file is missing, or over 3 minutes old. The pipeline may be stopped, or the laptop may have slept | Check that the pipeline is running. Spend still reads from the ledger |
| "The local file is gone" | Archive files move to a private S3 bucket and leave this machine after 24 hours. These views do not fetch from S3 | Trace a newer item |
| A section has no trace or decision | The item is older than capture, which started on the afternoon of November 1 | Trace a newer item |
| Rows catalog and rows scan differ slightly | The catalog is current, and the scan is as old as the section heading says | Nothing |
