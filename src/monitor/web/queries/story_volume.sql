-- Items per bin and primary level. Every level has a row in every bin.
WITH base AS (
    SELECT time_bucket(INTERVAL '{bin} minutes', f.first_seen_at) AS bin_start,
           f.primary_level AS level
    FROM facts f
    WHERE {where} AND f.storyline_id = $storyline_id
),
bins AS (
    SELECT unnest(generate_series(
        time_bucket(INTERVAL '{bin} minutes', $window_start::TIMESTAMPTZ),
        time_bucket(INTERVAL '{bin} minutes', $as_of::TIMESTAMPTZ),
        INTERVAL '{bin} minutes'
    )) AS bin_start
),
levels AS (
    SELECT unnest(['national', 'state', 'local', 'none']) AS level
),
counts AS (SELECT bin_start, level, count(*) AS n FROM base GROUP BY 1, 2)
SELECT b.bin_start, b.bin_start + INTERVAL '{bin} minutes' AS bin_end,
       l.level, coalesce(c.n, 0) AS n
FROM bins b CROSS JOIN levels l
LEFT JOIN counts c USING (bin_start, level)
ORDER BY b.bin_start,
         array_position(['national', 'state', 'local', 'none'], l.level)
