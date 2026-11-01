-- One state's items and all items per bin, for the attention index.
WITH base AS (
    SELECT time_bucket(INTERVAL '{bin} minutes', f.first_seen_at) AS bin_start,
           list_contains(f.states, $usps) AS in_state
    FROM facts f
    WHERE {where}
),
bins AS (
    SELECT unnest(generate_series(
        time_bucket(INTERVAL '{bin} minutes', $window_start::TIMESTAMPTZ),
        time_bucket(INTERVAL '{bin} minutes', $as_of::TIMESTAMPTZ),
        INTERVAL '{bin} minutes'
    )) AS bin_start
),
counts AS (
    SELECT bin_start, count(*) FILTER (WHERE in_state) AS n, count(*) AS total
    FROM base GROUP BY 1
)
SELECT b.bin_start, coalesce(c.n, 0) AS n, coalesce(c.total, 0) AS total
FROM bins b LEFT JOIN counts c USING (bin_start)
ORDER BY b.bin_start
