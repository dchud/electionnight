-- Mentions of one entity per bin. Every bin has a row.
WITH base AS (
    SELECT time_bucket(INTERVAL '{bin} minutes', f.first_seen_at) AS bin_start
    FROM mentions m JOIN facts f USING (item_id)
    WHERE {where} AND m.entity_id = $entity_id
),
bins AS (
    SELECT unnest(generate_series(
        time_bucket(INTERVAL '{bin} minutes', $window_start::TIMESTAMPTZ),
        time_bucket(INTERVAL '{bin} minutes', $as_of::TIMESTAMPTZ),
        INTERVAL '{bin} minutes'
    )) AS bin_start
),
counts AS (SELECT bin_start, count(*) AS n FROM base GROUP BY 1)
SELECT b.bin_start, b.bin_start + INTERVAL '{bin} minutes' AS bin_end,
       coalesce(c.n, 0) AS n
FROM bins b LEFT JOIN counts c USING (bin_start)
ORDER BY b.bin_start
