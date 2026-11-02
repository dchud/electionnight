-- Newest items matching the global filters.
SELECT f.item_id, f.headline, f.title, f.url, f.language, f.first_seen_at,
       f.published_at, f.primary_level, f.states, f.tone, f.topics,
       f.storyline_id, f.is_opinion,
       coalesce(s.name, f.source_id) AS outlet, s.source_id, s.lean_label,
       s.country, s.state_affiliated
FROM facts f
LEFT JOIN sources s USING (source_id)
WHERE {where}
ORDER BY f.first_seen_at DESC, f.item_id
LIMIT $limit
