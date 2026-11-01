-- Arrivals per bin. `first_seen_at` in the fact table is the dashboard axis;
-- `seen_raw_at` is the true first sighting, which gold.* bins use.
WITH bins AS (
    SELECT unnest(generate_series(
        time_bucket(INTERVAL ({minutes}) MINUTE, min(seen_raw_at)),
        time_bucket(INTERVAL ({minutes}) MINUTE, max(seen_raw_at)),
        INTERVAL ({minutes}) MINUTE
    )) AS bin_start
    FROM facts
),
seen AS (
    SELECT time_bucket(INTERVAL ({minutes}) MINUTE, seen_raw_at) AS bin_start,
           count(*) AS n,
           count(*) FILTER (
               WHERE published_at < seen_raw_at - INTERVAL 2 HOUR
           ) AS backlog
    FROM facts GROUP BY 1
),
axis AS (
    SELECT time_bucket(INTERVAL ({minutes}) MINUTE, first_seen_at) AS bin_start,
           count(*) AS on_axis
    FROM facts GROUP BY 1
),
first_fetch AS (
    SELECT time_bucket(
               INTERVAL ({minutes}) MINUTE, min(seen_raw_at)
           ) AS bin_start,
           source_id
    FROM facts GROUP BY source_id
),
outlets AS (
    SELECT bin_start, count(*) AS outlets_first
    FROM first_fetch GROUP BY 1
)
SELECT b.bin_start, coalesce(s.n, 0) AS n,
       coalesce(s.backlog, 0) AS backlog,
       coalesce(s.n, 0) - coalesce(s.backlog, 0) AS fresh,
       coalesce(a.on_axis, 0) AS on_axis,
       coalesce(o.outlets_first, 0) AS outlets_first
FROM bins b
LEFT JOIN seen s USING (bin_start)
LEFT JOIN axis a USING (bin_start)
LEFT JOIN outlets o USING (bin_start)
ORDER BY b.bin_start
