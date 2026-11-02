-- Mention tone per lean bucket, for the 10 most-mentioned entities. Opinion
-- items are excluded.
WITH base AS (
    SELECT m.entity_id, m.tone, coalesce(s.lean_label, 'Unrated') AS bucket
    FROM mentions m
    JOIN facts f USING (item_id)
    LEFT JOIN sources s USING (source_id)
    WHERE {where} AND m.entity_id IS NOT NULL AND NOT f.is_opinion
),
top_entities AS (
    SELECT entity_id FROM base GROUP BY 1
    ORDER BY count(*) DESC, entity_id LIMIT 10
),
grouped AS (
    SELECT entity_id, bucket, sum(tone) AS tone_sum,
           sum(tone * tone) AS tone_sq, count(tone) AS tone_n
    FROM base WHERE bucket IN ('L', 'LL', 'C', 'LR', 'R')
    GROUP BY ALL
    UNION ALL
    SELECT entity_id, 'All', sum(tone), sum(tone * tone), count(tone)
    FROM base GROUP BY 1
)
SELECT g.*, coalesce(e.canonical_name, g.entity_id) AS entity
FROM grouped g
JOIN top_entities t USING (entity_id)
LEFT JOIN entities e USING (entity_id)
