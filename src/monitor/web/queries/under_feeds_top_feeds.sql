-- The feeds that added the most items. A feed gets credit only when it was
-- first to serve an item, so `sightings` shows what it also carried.
WITH polls AS (
    SELECT feed_url, any_value(source_id) AS source_id, count(*) AS polls,
           coalesce(sum(n_entries), 0) AS entries_served,
           coalesce(sum(n_new), 0) AS items_kept
    FROM lake.raw.fetches
    GROUP BY 1
),
seen AS (
    SELECT s.feed_url, count(*) AS sightings
    FROM lake.raw.sightings s
    GROUP BY 1
)
SELECT p.feed_url, p.source_id, p.polls, p.entries_served, p.items_kept,
       p.items_kept * 100.0 / p.polls AS new_per_100_polls,
       coalesce(s.sightings, 0) AS sightings
FROM polls p
LEFT JOIN seen s USING (feed_url)
ORDER BY p.items_kept DESC, p.feed_url
LIMIT 20
