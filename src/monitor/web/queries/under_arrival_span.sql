-- The first and last arrival, and how much sits before the first arrival on
-- the dashboard's time axis. An item is moved on that axis for one of two
-- reasons: backlog, or publication around a collection gap.
SELECT min(seen_raw_at) AS first_seen, max(seen_raw_at) AS last_seen,
       count(*) AS items,
       count(*) FILTER (WHERE published_at < seen_raw_at - INTERVAL 2 HOUR)
           AS backlog,
       count(*) FILTER (
           WHERE first_seen_at <> seen_raw_at
             AND NOT coalesce(
                 published_at < seen_raw_at - INTERVAL 2 HOUR, FALSE
             )
       ) AS moved_for_gap,
       count(*) FILTER (
           WHERE first_seen_at < (SELECT min(seen_raw_at) FROM facts)
       ) AS before_first_seen,
       count(*) FILTER (
           WHERE seen_raw_at < (SELECT min(seen_raw_at) FROM facts)
               + INTERVAL 15 MINUTE
       ) AS first_15_minutes
FROM facts
