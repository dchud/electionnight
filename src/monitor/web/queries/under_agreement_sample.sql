-- A stable sample of items the model classified, ordered by a hash of the id,
-- with the raw text the keyword path reads.
WITH latest AS (
    SELECT item_id, election_relevant, states, topics, primary_level, method
    FROM lake.nlp.classify_a
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
),
lang AS (
    SELECT item_id, language
    FROM lake.nlp.lang
    QUALIFY row_number() OVER (
        PARTITION BY item_id ORDER BY created_at DESC) = 1
)
SELECT i.item_id, i.source_id, i.title, i.summary, i.url, i.feed_categories,
       coalesce(l.language, i.language, 'en') AS language,
       c.election_relevant, c.states, c.topics, c.primary_level
FROM latest c
JOIN lake.raw.items i USING (item_id)
LEFT JOIN lang l USING (item_id)
WHERE c.method = 'llm'
ORDER BY hash(i.item_id)
LIMIT {sample}
