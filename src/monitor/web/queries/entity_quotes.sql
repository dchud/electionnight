-- Evidence quotes for one entity, most negative or most positive first.
SELECT m.tone, m.tone_evidence, m.surface, f.item_id, f.first_seen_at,
       f.headline, coalesce(s.name, f.source_id) AS outlet, f.source_id,
       s.lean_label, s.country
FROM mentions m
JOIN facts f USING (item_id)
LEFT JOIN sources s USING (source_id)
WHERE {where} AND m.entity_id = $entity_id
  AND m.tone {compare} 0 AND coalesce(m.tone_evidence, '') <> ''
  AND NOT f.is_opinion
ORDER BY m.tone {order}, f.first_seen_at DESC, f.item_id
LIMIT $limit
