-- The archived feed payload nearest the moment the item was first seen.
WITH item AS (
    SELECT feed_url, first_seen_at FROM lake.raw.items
    WHERE item_id = $item_id LIMIT 1
)
SELECT a.*
FROM lake.raw.archive_index a, item i
WHERE a.kind = 'feeds' AND a.key = i.feed_url
  AND a.fetched_at >= i.first_seen_at - INTERVAL 1 MINUTE
ORDER BY abs(epoch(a.fetched_at) - epoch(i.first_seen_at))
LIMIT 1
