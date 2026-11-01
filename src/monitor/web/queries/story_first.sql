-- The first canonical report of one storyline.
SELECT f.first_seen_at, f.item_id,
       coalesce(s.name, f.source_id) AS outlet
FROM facts f LEFT JOIN sources s USING (source_id)
WHERE {where} AND f.storyline_id = $storyline_id
ORDER BY f.first_seen_at, f.item_id
LIMIT 1
