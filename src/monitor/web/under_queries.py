"""A gallery of example queries over what the lake holds.

Each query is a fixed string that passes the SQL console's allowlist, so the
gallery's link can open it there unchanged. None selects full article text:
long text is cut to an excerpt, and a vector shows its length and first values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from monitor.web.under import SLOW_SCAN_TTL_SECONDS, Under, failure_lines

RESULT_ROWS = 15

# Two groupings, because "model types" can mean either.
SHAPES = {
    "json": "Nested feed JSON",
    "lists": "List columns",
    "vectors": "Vectors",
    "graph": "A graph from a self-join",
    "latest": "Append-only reading: the latest-row rule",
    "time": "Time travel and changes between snapshots",
    "catalog": "Catalog and file metadata",
    "columns": "Plain columns",
}
PRODUCERS = {
    "feeds": "The feeds themselves, with no model",
    "lang": "The local language detector",
    "embed": "The local embedding model",
    "classify": "Classification by the bulk model",
    "extract": "Extraction by the bulk model",
    "briefs": "Briefs by the brief model",
    "keywords": "The keyword path",
    "cost": "Every model: the ledger",
    "system": "The pipeline and DuckLake, with no model",
}


@dataclass(frozen=True)
class GalleryQuery:
    key: str
    shape: str
    producer: str
    question: str
    sql: str
    interesting: str
    not_mean: str

    def text(self, older: int, newer: int) -> str:
        """The SQL as it runs, with any snapshot numbers filled in."""
        return (
            self.sql.replace("{older}", str(older))
            .replace("{newer}", str(newer))
            .strip()
        )


QUERIES = (
    GalleryQuery(
        key="feed_fields",
        shape="json",
        producer="feeds",
        question="Which fields do feed entries carry, and how often is each "
        "one filled?",
        sql="""
-- raw_entry is JSON. json_keys lists the top-level names in each row.
WITH fields AS (
    SELECT unnest(json_keys(raw_entry)) AS field, raw_entry
    FROM lake.raw.items
    WHERE raw_entry IS NOT NULL
)
SELECT field, count(*) AS entries,
       count(*) FILTER (
           WHERE coalesce(raw_entry ->> field, '') NOT IN ('', '[]')
       ) AS filled
FROM fields
GROUP BY field
ORDER BY entries DESC, field
LIMIT 12
""",
        interesting="One JSON column keeps whatever a feed sent, so a field "
        "the parser ignores can still be read here, with no schema change.",
        not_mean="A filled field is not a correct one. An outlet decides what "
        "goes in it.",
    ),
    GalleryQuery(
        key="feed_tags",
        shape="json",
        producer="feeds",
        question="Which tags do outlets put on their own stories?",
        sql="""
-- Reach into the JSON, then turn a JSON array into a list and unnest it.
SELECT tag, count(*) AS entries
FROM (
    SELECT unnest(from_json(raw_entry -> '$.tags', '["VARCHAR"]')) AS tag
    FROM lake.raw.items
    WHERE raw_entry IS NOT NULL
)
WHERE tag IS NOT NULL
GROUP BY tag
ORDER BY entries DESC, tag
LIMIT 12
""",
        interesting="Each outlet has its own tag vocabulary. This is what the "
        "classifier's closed topic list replaces.",
        not_mean="These are not topics in any shared sense. Two outlets can "
        "use one tag differently, and most early items are backlog.",
    ),
    GalleryQuery(
        key="feed_text_size",
        shape="json",
        producer="feeds",
        question="How much text does a feed entry carry, by outlet?",
        sql="""
-- Lengths only. This reads the text and returns no text.
SELECT source_id, count(*) AS entries,
       round(avg(length(coalesce(raw_entry ->> '$.summary', ''))))
           AS mean_summary_chars,
       round(avg(length(coalesce(raw_entry ->> '$.content', ''))))
           AS mean_content_chars
FROM lake.raw.items
WHERE raw_entry IS NOT NULL
GROUP BY source_id
ORDER BY mean_content_chars DESC, source_id
LIMIT 10
""",
        interesting="Some feeds send a sentence and some send the article, "
        "which is why the pipeline fetches pages for relevant items.",
        not_mean="A long entry is not a better story. Entries over the size "
        "cap are cut when stored.",
    ),
    GalleryQuery(
        key="brief_citations",
        shape="json",
        producer="briefs",
        question="How many items does each brief cite?",
        sql="""
-- citations is a JSON object from reference number to item id.
SELECT left(brief_id, 12) AS brief, focus, model,
       len(json_keys(citations)) AS items_cited,
       created_at
FROM lake.nlp.briefs
ORDER BY created_at DESC
LIMIT 10
""",
        interesting="The citation map is what lets every brief sentence link "
        "to the stories it rests on.",
        not_mean="A citation is checked to be a supplied item. It is not "
        "checked to support the claim.",
    ),
    GalleryQuery(
        key="list_topics",
        shape="lists",
        producer="classify",
        question="Which topics do relevant stories carry?",
        sql="""
-- Take each item's newest classification, then unnest its list of topics.
WITH latest AS (
    SELECT item_id, election_relevant, topics
    FROM lake.nlp.classify_a
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT topic, count(*) AS items
FROM (
    SELECT unnest(topics) AS topic FROM latest WHERE election_relevant
)
GROUP BY topic
ORDER BY items DESC, topic
LIMIT 12
""",
        interesting="A list column turns into rows with one function, with "
        "no join table.",
        not_mean="One item can carry several topics, so these do not sum to "
        "the number of items. All copies count here, including backlog.",
    ),
    GalleryQuery(
        key="list_state_pairs",
        shape="lists",
        producer="classify",
        question="Which pairs of states appear together in one story?",
        sql="""
WITH latest AS (
    SELECT item_id, election_relevant, states
    FROM lake.nlp.classify_a
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
tagged AS (
    SELECT item_id, unnest(states) AS state
    FROM latest
    WHERE election_relevant
)
SELECT a.state AS first_state, b.state AS second_state, count(*) AS items
FROM tagged a
JOIN tagged b ON a.item_id = b.item_id AND a.state < b.state
GROUP BY ALL
ORDER BY items DESC, first_state, second_state
LIMIT 10
""",
        interesting="Unnesting and joining a list to itself gives pairs, the "
        "start of a graph.",
        not_mean="Named together does not mean related. A national roundup "
        "names many states.",
    ),
    GalleryQuery(
        key="vector_shape",
        shape="vectors",
        producer="embed",
        question="What does a stored vector look like?",
        sql="""
-- Vectors are FLOAT[] in the lake. len() and a slice show one without printing
-- all of it.
SELECT model, text_basis, dim, len(vec) AS vector_length,
       count(*) AS n_rows,
       [round(value, 3) for value in any_value(vec)[1:4]] AS first_values
FROM lake.nlp.embeddings
GROUP BY model, text_basis, dim, len(vec)
ORDER BY n_rows DESC
""",
        interesting="A column of lists of floats is all a vector store needs "
        "here. Search reads it into memory on the web process.",
        not_mean="No single value means anything alone. Only the angle "
        "between two vectors does.",
    ),
    GalleryQuery(
        key="vector_neighbors",
        shape="vectors",
        producer="embed",
        question="Which stories sit closest to the newest story?",
        sql="""
-- Compare one vector with the newest 5,000, by cosine similarity.
WITH newest AS (
    SELECT item_id, vec
    FROM lake.nlp.embeddings
    ORDER BY created_at DESC
    LIMIT 5000
),
target AS (SELECT item_id, vec FROM newest LIMIT 1)
SELECT round(list_cosine_similarity(t.vec, n.vec), 3) AS cosine,
       n.item_id, left(i.title, 90) AS title
FROM target t, newest n
JOIN lake.raw.items i ON i.item_id = n.item_id
ORDER BY cosine DESC, n.item_id
LIMIT 8
""",
        interesting="Nearest neighbors in plain SQL, with no index, because a "
        "bounded set of vectors is small enough to scan.",
        not_mean="Close in meaning is not the same event, and not true. The "
        "vector covers the title and summary, not the full text.",
    ),
    GalleryQuery(
        key="vector_english_spanish",
        shape="vectors",
        producer="embed",
        question="Which Spanish stories are closest in meaning to English "
        "stories?",
        sql="""
-- Each item's newest language row decides which side it is on. A Spanish
-- item's vector comes from the English the classifier wrote, if it wrote any.
WITH language AS (
    SELECT item_id, language
    FROM lake.nlp.lang
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
vectors AS (
    SELECT e.item_id, e.vec, l.language, e.created_at
    FROM lake.nlp.embeddings e
    JOIN language l USING (item_id)
    WHERE l.language IN ('en', 'es')
),
english AS (
    SELECT item_id, vec FROM vectors WHERE language = 'en'
    ORDER BY created_at DESC LIMIT 500
),
spanish AS (
    SELECT item_id, vec FROM vectors WHERE language = 'es'
    ORDER BY created_at DESC LIMIT 500
),
pairs AS (
    SELECT en.item_id AS en_id, es.item_id AS es_id,
           list_cosine_similarity(en.vec, es.vec) AS cosine
    FROM english en, spanish es
    ORDER BY cosine DESC
    LIMIT 8
)
SELECT round(p.cosine, 3) AS cosine, left(a.title, 70) AS english_title,
       left(b.title, 70) AS spanish_title
FROM pairs p
JOIN lake.raw.items a ON a.item_id = p.en_id
JOIN lake.raw.items b ON b.item_id = p.es_id
ORDER BY cosine DESC
""",
        interesting="An English story and a Spanish story about one event "
        "land near each other. The classifier's English translation gets them "
        "there, and the model handles Spanish directly when a search is typed "
        "in Spanish.",
        not_mean="A high cosine here rests partly on that translation. It "
        "does not show that the two stories agree, and the top pairs can be "
        "copies of one wire story.",
    ),
    GalleryQuery(
        key="vector_decisions",
        shape="vectors",
        producer="embed",
        question="How close did items come to joining an existing storyline?",
        sql="""
-- Each storyline decision keeps the nearest cosine and the threshold in force.
SELECT decision, floor(nearest_similarity * 20) / 20 AS cosine_from,
       count(*) AS decisions, any_value(threshold) AS threshold
FROM lake.nlp.cluster_decisions
WHERE stage = 'storylines' AND nearest_similarity IS NOT NULL
GROUP BY ALL
ORDER BY cosine_from DESC, decision
LIMIT 15
""",
        interesting="A threshold becomes visible: every item that joined sits "
        "at or above it, and every item that started a storyline sits below.",
        not_mean="These are rows, and a parser version change writes new "
        "ones. The threshold was tuned on similarity, not checked against "
        "labels.",
    ),
    GalleryQuery(
        key="graph_pairs",
        shape="graph",
        producer="extract",
        question="Which entities are named together most often?",
        sql="""
-- Join mentions to themselves on item_id to get an edge list.
WITH named AS (
    SELECT item_id, entity_id
    FROM lake.nlp.mentions
    WHERE entity_id IS NOT NULL
    QUALIFY row_number() OVER (
        PARTITION BY mention_id ORDER BY created_at DESC) = 1
),
edges AS (
    SELECT a.entity_id AS first_id, b.entity_id AS second_id,
           count(DISTINCT a.item_id) AS items
    FROM named a
    JOIN named b ON a.item_id = b.item_id AND a.entity_id < b.entity_id
    GROUP BY ALL
),
names AS (
    SELECT entity_id, canonical_name
    FROM lake.ref.entities
    QUALIFY row_number() OVER (
        PARTITION BY entity_id ORDER BY created_at DESC) = 1
)
SELECT f.canonical_name AS first_entity, s.canonical_name AS second_entity,
       e.items
FROM edges e
JOIN names f ON f.entity_id = e.first_id
JOIN names s ON s.entity_id = e.second_id
ORDER BY e.items DESC, first_entity, second_entity
LIMIT 10
""",
        interesting="A relational table holds a graph: a self-join makes the "
        "edges, and no graph database is involved.",
        not_mean="Named in one story does not mean connected. All copies "
        "count here, so a syndicated story counts once per copy.",
    ),
    GalleryQuery(
        key="graph_degree",
        shape="graph",
        producer="system",
        question="Which entity is named alongside the most other entities?",
        sql="""
-- gold.comention_edges holds the edges per 5-minute bin. Take each bin's
-- newest row, list each edge both ways, and count distinct neighbors.
WITH newest AS (
    SELECT entity_a, entity_b, n
    FROM lake.gold.comention_edges
    QUALIFY row_number() OVER (
        PARTITION BY bin_start, entity_a, entity_b
        ORDER BY created_at DESC) = 1
),
both_ways AS (
    SELECT entity_a AS entity_id, entity_b AS neighbor, n FROM newest
    UNION ALL
    SELECT entity_b, entity_a, n FROM newest
),
names AS (
    SELECT entity_id, canonical_name
    FROM lake.ref.entities
    QUALIFY row_number() OVER (
        PARTITION BY entity_id ORDER BY created_at DESC) = 1
)
SELECT coalesce(s.canonical_name, b.entity_id) AS entity,
       count(DISTINCT b.neighbor) AS neighbors, sum(b.n) AS co_mentions
FROM both_ways b
LEFT JOIN names s USING (entity_id)
GROUP BY ALL
ORDER BY neighbors DESC, entity
LIMIT 10
""",
        interesting="A degree count over a rollup table, which is cheaper "
        "than rebuilding the edges from mentions.",
        not_mean="A high degree can mean a person named in roundups. It does "
        "not measure importance.",
    ),
    GalleryQuery(
        key="latest_naive_sum",
        shape="latest",
        producer="system",
        question="What happens if a query ignores the latest-row rule?",
        sql="""
-- A recomputed bin is a new row, so a plain sum counts every recompute.
WITH newest AS (
    SELECT n_items
    FROM lake.gold.source_bins
    QUALIFY row_number() OVER (
        PARTITION BY bin_start, source_id ORDER BY created_at DESC) = 1
)
SELECT (SELECT count(*) FROM lake.gold.source_bins) AS rows_stored,
       (SELECT count(*) FROM newest) AS distinct_bins,
       (SELECT sum(n_items) FROM lake.gold.source_bins) AS naive_sum,
       (SELECT sum(n_items) FROM newest) AS latest_row_sum,
       (SELECT count(*) FROM lake.raw.items) AS items_in_raw
""",
        interesting="The same table gives two answers, and only one is the "
        "number of items.",
        not_mean="The two sums are close when bins rarely change. A small gap "
        "does not make the naive sum safe.",
    ),
    GalleryQuery(
        key="latest_versions",
        shape="latest",
        producer="classify",
        question="How many versions of a storyline does the lake keep?",
        sql="""
-- Each growth or relabel appends a version. arg_max reads the newest label.
SELECT left(storyline_id, 12) AS storyline, count(*) AS versions,
       left(arg_max(label, version), 70) AS newest_label,
       max(n_items) AS most_items
FROM lake.nlp.storylines
GROUP BY storyline_id
ORDER BY versions DESC, storyline
LIMIT 8
""",
        interesting="Every label a storyline has had is still in the lake, so "
        "a query can show how a story's name changed.",
        not_mean="A count of rows here is a count of writes, not storylines. "
        "More versions means more writes, not a bigger story.",
    ),
    GalleryQuery(
        key="latest_parser_runs",
        shape="latest",
        producer="system",
        question="How many items sit in each state for each parser?",
        sql="""
-- Each item's newest run per parser. An item that failed and then succeeded
-- counts once, as ok.
SELECT parser, status, count(*) AS items
FROM (
    SELECT parser, status
    FROM lake.nlp.parser_runs
    QUALIFY row_number() OVER (
        PARTITION BY item_id, parser ORDER BY created_at DESC) = 1
)
GROUP BY ALL
ORDER BY parser, items DESC
""",
        interesting="The latest-row rule over a log of events gives current "
        "state, with no status column to keep up to date.",
        not_mean="`ok` means the parser finished. It does not mean its answer "
        "is right.",
    ),
    GalleryQuery(
        key="time_two_snapshots",
        shape="time",
        producer="system",
        question="How has one table grown between two snapshots?",
        sql="""
-- AT (VERSION => n) reads the table as it stood at that commit.
SELECT 'older' AS read_at, {older} AS version, count(*) AS n_rows,
       count(DISTINCT storyline_id) AS storylines
FROM lake.nlp.storylines AT (VERSION => {older})
UNION ALL
SELECT 'newer', {newer}, count(*), count(DISTINCT storyline_id)
FROM lake.nlp.storylines AT (VERSION => {newer})
""",
        interesting="An old answer stays exact, even after maintenance "
        "rewrote the files it came from.",
        not_mean="A snapshot is a commit, so its number says nothing about "
        "wall-clock time, and rows are not records in this table.",
    ),
    GalleryQuery(
        key="time_changes",
        shape="time",
        producer="system",
        question="Which rows did the last snapshots add to one table?",
        sql="""
-- table_changes lists rows written between two snapshots, without a scan of
-- the whole table.
SELECT snapshot_id, change_type, left(storyline_id, 12) AS storyline, version,
       left(label, 60) AS label
FROM ducklake_table_changes('lake', 'nlp', 'storylines', {older}, {newer})
ORDER BY snapshot_id DESC, storyline
LIMIT 10
""",
        interesting="Change data with no extra machinery, because each row "
        "records its snapshot.",
        not_mean="In this append-only table every change is an insert. An "
        "insert here can be a new version of a storyline, not a new one.",
    ),
    GalleryQuery(
        key="catalog_nested",
        shape="catalog",
        producer="system",
        question="Which columns hold nested types, according to the catalog?",
        sql="""
-- A list column has a child row in ducklake_column, tied by parent_column.
SELECT s.schema_name || '.' || t.table_name AS lake_table,
       p.column_name, p.column_type, c.column_name AS child, c.column_type
           AS child_type
FROM __ducklake_metadata_lake.main.ducklake_column c
JOIN __ducklake_metadata_lake.main.ducklake_column p
    ON p.column_id = c.parent_column AND p.table_id = c.table_id
JOIN __ducklake_metadata_lake.main.ducklake_table t ON t.table_id = c.table_id
JOIN __ducklake_metadata_lake.main.ducklake_schema s USING (schema_id)
WHERE c.end_snapshot IS NULL AND p.end_snapshot IS NULL
  AND t.end_snapshot IS NULL
ORDER BY lake_table, p.column_order
LIMIT 15
""",
        interesting="The schema is rows in SQLite, so plain SQL can ask about "
        "the schema itself.",
        not_mean="A type constrains shape only. It does not show the list's "
        "length or that its values are sensible.",
    ),
    GalleryQuery(
        key="catalog_column_sizes",
        shape="catalog",
        producer="system",
        question="Which columns take the most space on disk?",
        sql="""
-- Sizes come from the catalog. No data file is opened.
SELECT s.schema_name || '.' || t.table_name AS lake_table, c.column_name,
       sum(st.column_size_bytes) AS bytes_on_disk, count(*) AS files
FROM __ducklake_metadata_lake.main.ducklake_file_column_stats st
JOIN __ducklake_metadata_lake.main.ducklake_data_file f
    USING (data_file_id, table_id)
JOIN __ducklake_metadata_lake.main.ducklake_column c
    ON c.column_id = st.column_id AND c.table_id = st.table_id
JOIN __ducklake_metadata_lake.main.ducklake_table t ON t.table_id = st.table_id
JOIN __ducklake_metadata_lake.main.ducklake_schema s USING (schema_id)
WHERE f.end_snapshot IS NULL AND c.end_snapshot IS NULL
  AND t.end_snapshot IS NULL
GROUP BY ALL
ORDER BY bytes_on_disk DESC
LIMIT 12
""",
        interesting="The catalog answers a storage question in milliseconds, "
        "with no scan.",
        not_mean="Inlined rows are not in these sizes, and files awaiting "
        "cleanup are left out.",
    ),
    GalleryQuery(
        key="catalog_file_skipping",
        shape="catalog",
        producer="system",
        question="How many files would a query for the last hour open?",
        sql="""
-- A file whose newest created_at is older than the cutoff can be skipped,
-- because the catalog already knows each file's maximum.
SELECT s.schema_name || '.' || t.table_name AS lake_table, count(*) AS files,
       count(*) FILTER (
           WHERE TRY_CAST(st.max_value AS TIMESTAMPTZ)
               >= now() - INTERVAL 1 HOUR
       ) AS must_open
FROM __ducklake_metadata_lake.main.ducklake_file_column_stats st
JOIN __ducklake_metadata_lake.main.ducklake_data_file f
    USING (data_file_id, table_id)
JOIN __ducklake_metadata_lake.main.ducklake_column c
    ON c.column_id = st.column_id AND c.table_id = st.table_id
JOIN __ducklake_metadata_lake.main.ducklake_table t ON t.table_id = st.table_id
JOIN __ducklake_metadata_lake.main.ducklake_schema s USING (schema_id)
WHERE c.column_name = 'created_at' AND f.end_snapshot IS NULL
  AND c.end_snapshot IS NULL AND t.end_snapshot IS NULL
GROUP BY ALL
ORDER BY files DESC, lake_table
LIMIT 12
""",
        interesting="Column statistics are how a lake can skip most of its "
        "data. A merged file holding old and new rows has to be opened.",
        not_mean="This counts files, not bytes, and inlined rows are always "
        "read. Skipping helps a filter on a column with a narrow range in "
        "each file.",
    ),
    GalleryQuery(
        key="lang_vs_feed",
        shape="columns",
        producer="lang",
        question="How often does the detector overrule the feed's language?",
        sql="""
WITH language AS (
    SELECT item_id, language, method
    FROM lake.nlp.lang
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT i.language AS declared, l.language AS decided, l.method,
       count(*) AS items
FROM language l
JOIN lake.raw.items i USING (item_id)
GROUP BY ALL
ORDER BY items DESC, declared, decided
LIMIT 12
""",
        interesting="Two sources of one fact, in two tables, side by side. A "
        "row where they differ shows the detector's rule at work.",
        not_mean="`detected` means the detector's rule fired. It does not "
        "mean the detector is right, and no sample was checked by hand.",
    ),
    GalleryQuery(
        key="lang_confidence",
        shape="columns",
        producer="lang",
        question="How confident is the detector, by language?",
        sql="""
WITH language AS (
    SELECT item_id, language, confidence
    FROM lake.nlp.lang
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT language, count(*) AS items, round(avg(confidence), 3) AS mean,
       round(min(confidence), 3) AS lowest
FROM language
GROUP BY language
ORDER BY items DESC, language
LIMIT 12
""",
        interesting="The detector costs nothing to run, and its confidence is "
        "stored beside each answer.",
        not_mean="Confidence is the library's own score, not a measured error "
        "rate.",
    ),
    GalleryQuery(
        key="classify_confidence",
        shape="columns",
        producer="classify",
        question="How is the model's relevance confidence spread?",
        sql="""
WITH latest AS (
    SELECT election_relevant, relevance_conf
    FROM lake.nlp.classify_a
    WHERE method = 'llm'
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT election_relevant, floor(relevance_conf * 5) / 5 AS conf_from,
       count(*) AS items
FROM latest
GROUP BY ALL
ORDER BY election_relevant, conf_from
""",
        interesting="A model's score has a shape. A pile at the extremes "
        "shows how often the threshold of 0.5 decides anything.",
        not_mean="The score is the model's own report and is not calibrated. "
        "Most early items are backlog.",
    ),
    GalleryQuery(
        key="keyword_share",
        shape="columns",
        producer="keywords",
        question="How many items did each path classify, and how many did it "
        "call relevant?",
        sql="""
WITH latest AS (
    SELECT method, election_relevant
    FROM lake.nlp.classify_a
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT method, count(*) AS items,
       count(*) FILTER (WHERE election_relevant) AS relevant,
       round(avg(election_relevant::INT), 3) AS relevant_share
FROM latest
GROUP BY method
ORDER BY items DESC
""",
        interesting="The fallback path is stored beside the model's path, so "
        "its share is visible at any moment.",
        not_mean="These are different items, so the shares cannot compare the "
        "two paths. The Models view runs both on one sample.",
    ),
    GalleryQuery(
        key="keyword_agreement",
        shape="columns",
        producer="keywords",
        question="Where one item has both a keyword row and a model row, do "
        "they agree?",
        sql="""
SELECT k.election_relevant AS keywords_relevant,
       l.election_relevant AS model_relevant,
       count(DISTINCT item_id) AS items
FROM lake.nlp.classify_a k
JOIN lake.nlp.classify_a l USING (item_id)
WHERE k.method = 'keywords' AND l.method = 'llm'
GROUP BY ALL
ORDER BY items DESC
""",
        interesting="Append-only data keeps both answers when an item is "
        "classified twice, so a self-join can compare them at no cost.",
        not_mean="Few items go both ways, and those are not a random sample. "
        "Neither column is ground truth.",
    ),
    GalleryQuery(
        key="extract_basis",
        shape="columns",
        producer="extract",
        question="How often did the model read the article, not just the "
        "summary?",
        sql="""
WITH latest AS (
    SELECT text_basis, article_tone
    FROM lake.nlp.extract_b
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT text_basis, count(*) AS items, round(avg(article_tone), 3) AS raw_mean,
       round(stddev_samp(article_tone), 3) AS std_dev
FROM latest
GROUP BY text_basis
ORDER BY items DESC
""",
        interesting="The evidence behind a value is itself a column, so a "
        "query can split on it.",
        not_mean="These are different stories, so a gap does not measure "
        "what reading more text does. The dashboard reports tone per entity "
        "with an interval, not this mean.",
    ),
    GalleryQuery(
        key="extract_entity_tone",
        shape="columns",
        producer="extract",
        question="What is the raw mean tone for the most-named entities?",
        sql="""
-- Opinion items are left out, as they are on the dashboard.
WITH opinion AS (
    SELECT item_id, is_opinion
    FROM lake.nlp.classify_a
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
latest AS (
    SELECT m.entity_id, m.tone
    FROM lake.nlp.mentions m
    JOIN opinion o USING (item_id)
    WHERE m.entity_id IS NOT NULL AND NOT coalesce(o.is_opinion, FALSE)
    QUALIFY row_number() OVER (
        PARTITION BY m.mention_id ORDER BY m.created_at DESC) = 1
),
names AS (
    SELECT entity_id, canonical_name
    FROM lake.ref.entities
    QUALIFY row_number() OVER (
        PARTITION BY entity_id ORDER BY created_at DESC) = 1
)
SELECT n.canonical_name AS entity, count(l.tone) AS n,
       round(avg(l.tone), 2) AS raw_mean, round(stddev_samp(l.tone), 2) AS s
FROM latest l
JOIN names n USING (entity_id)
GROUP BY entity
HAVING count(l.tone) >= 5
ORDER BY n DESC, entity
LIMIT 10
""",
        interesting="Extraction turned sentences into numbers per person, and "
        "a mean over them is one line of SQL.",
        not_mean="Tone is how articles portray someone, not whether the news "
        "is good for them. The dashboard adds a 95% t-interval and flags n "
        "under 15. Copies are not independent, so n overstates the evidence.",
    ),
    GalleryQuery(
        key="extract_rejected",
        shape="columns",
        producer="extract",
        question="How often does a check throw out what the model said?",
        sql="""
SELECT kind, reason, count(*) AS rows_kept
FROM lake.nlp.rejected_quotes
GROUP BY ALL
ORDER BY rows_kept DESC, kind, reason
LIMIT 10
""",
        interesting="Rejected output is data too, kept as rows, so the checks "
        "can themselves be checked.",
        not_mean="A rejected quote is not shown to be wrong, only unverified. "
        "These rules also drop some true claims.",
    ),
    GalleryQuery(
        key="brief_sentences",
        shape="columns",
        producer="briefs",
        question="How many sentences survive the citation check in each "
        "brief?",
        sql="""
SELECT left(b.brief_id, 12) AS brief, b.focus, s.sentences_kept,
       s.sentences_dropped, s.citations_dropped, b.created_at
FROM lake.nlp.brief_stats s
JOIN lake.nlp.briefs b USING (brief_id)
ORDER BY b.created_at DESC
LIMIT 10
""",
        interesting="The brief model's output is cut by code before anyone "
        "sees it, and the cuts are recorded.",
        not_mean="A kept sentence cites a supplied item. That does not show "
        "the item supports it.",
    ),
    GalleryQuery(
        key="cost_per_model",
        shape="columns",
        producer="cost",
        question="What has each model cost, per purpose and per call?",
        sql="""
SELECT model, purpose, count(*) AS calls,
       round(sum(usd_est), 4) AS usd_est,
       round(sum(usd_est) / count(*), 6) AS usd_est_per_call,
       sum(cache_read_tokens) AS cache_read_tokens,
       sum(input_tokens) AS input_tokens,
       sum(output_tokens) AS output_tokens
FROM lake.nlp.llm_calls
GROUP BY model, purpose
ORDER BY usd_est DESC, model, purpose
""",
        interesting="Every model call leaves a ledger row, so spend can be "
        "cut by any column.",
        not_mean="`usd_est` is an estimate from configured prices, not an "
        "invoice, and SDK retries are not counted. `model` is the model "
        "requested.",
    ),
)


def _snapshots(under: Under) -> tuple[int, int]:
    """Two snapshots to compare: the newest, and up to 100 before it.

    The older one is never before the table the queries read existed.
    """
    newer = under.snapshot_id
    row = under.catalog(
        "SELECT t.begin_snapshot AS created "
        "FROM __ducklake_metadata_lake.main.ducklake_table t "
        "JOIN __ducklake_metadata_lake.main.ducklake_schema s USING "
        "(schema_id) WHERE s.schema_name = 'nlp' AND t.table_name = "
        "'storylines' AND t.end_snapshot IS NULL"
    ).first
    created = int(row.get("created") or 0)
    return max(created, newer - 100), newer


def gallery(under: Under, by: str) -> dict[str, Any]:
    """Every query with a bounded live result, grouped one way or the other."""
    group_by = "producer" if by == "producer" else "shape"
    names = PRODUCERS if group_by == "producer" else SHAPES
    older, newer = _snapshots(under)
    results = under.scan(
        "gallery",
        [(query.key, query.text(older, newer), None) for query in QUERIES],
        ttl=SLOW_SCAN_TTL_SECONDS,
        max_rows=RESULT_ROWS,
    )
    groups = []
    for key, title in names.items():
        members = [
            {"query": query, "ran": results[query.key]}
            for query in QUERIES
            if getattr(query, group_by) == key
        ]
        if members:
            groups.append({"key": key, "title": title, "members": members})
    return {
        "by": group_by,
        "groups": groups,
        "errors": failure_lines(results),
        "n_queries": len(QUERIES),
        "result_rows": RESULT_ROWS,
    }
