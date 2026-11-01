-- The newest items with an extraction trace and an archived page. Ordering by
-- first sighting keeps out old items that were only recently backfilled.
SELECT i.item_id, i.first_seen_at
FROM lake.raw.items i
WHERE i.item_id IN (
    SELECT unnest(item_ids) FROM lake.nlp.llm_traces
    WHERE purpose = 'extract_b'
)
  AND i.item_id IN (
    SELECT key FROM lake.raw.archive_index WHERE kind = 'pages'
)
ORDER BY i.first_seen_at DESC
LIMIT 8
