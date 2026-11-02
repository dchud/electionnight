-- Storylines whose items mention one entity.
SELECT f.storyline_id, st.label, st.status,
       count(DISTINCT f.item_id) AS n
FROM mentions m
JOIN facts f USING (item_id)
JOIN storylines st ON st.storyline_id = f.storyline_id
WHERE {where} AND m.entity_id = $entity_id
  AND coalesce(st.label, '') <> ''
GROUP BY ALL
ORDER BY n DESC, st.label
LIMIT 15
