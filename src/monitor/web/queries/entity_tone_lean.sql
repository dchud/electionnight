-- Tone toward one entity per lean bucket, plus all outlets. Opinion items are
-- excluded.
WITH base AS (
    SELECT m.tone, coalesce(s.lean_label, 'Unrated') AS bucket
    FROM mentions m
    JOIN facts f USING (item_id)
    LEFT JOIN sources s USING (source_id)
    WHERE {where} AND m.entity_id = $entity_id AND NOT f.is_opinion
)
SELECT bucket, count(*) AS n, sum(tone) AS tone_sum,
       sum(tone * tone) AS tone_sq, count(tone) AS tone_n
FROM base WHERE bucket IN ('L', 'LL', 'C', 'LR', 'R') GROUP BY 1
UNION ALL
SELECT 'All', count(*), sum(tone), sum(tone * tone), count(tone) FROM base
HAVING count(*) > 0
