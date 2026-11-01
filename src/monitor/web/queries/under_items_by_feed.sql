-- Items per feed URL. Feed URLs are not in the fact table, so this scans
-- one column of raw.items.
SELECT feed_url, any_value(source_id) AS source_id, count(*) AS items
FROM lake.raw.items
GROUP BY 1
ORDER BY items DESC, feed_url
LIMIT 25
