-- The first and last arrival, and how much sits before the first arrival on
-- the dashboard's time axis.
SELECT min(seen_raw_at) AS first_seen, max(seen_raw_at) AS last_seen,
       count(*) AS items,
       count(*) FILTER (WHERE published_at < seen_raw_at - INTERVAL 2 HOUR)
           AS backlog,
       count(*) FILTER (WHERE first_seen_at <> seen_raw_at) AS moved_on_axis,
       count(*) FILTER (
           WHERE first_seen_at < (SELECT min(seen_raw_at) FROM facts)
       ) AS before_first_seen,
       count(*) FILTER (
           WHERE seen_raw_at < (SELECT min(seen_raw_at) FROM facts)
               + INTERVAL 15 MINUTE
       ) AS first_15_minutes
FROM facts
