-- Other entities mentioned in the same items as one entity.
SELECT other.entity_id, coalesce(e.canonical_name, other.entity_id) AS entity,
       e.party, count(DISTINCT other.item_id) AS n
FROM mentions m
JOIN mentions other USING (item_id)
JOIN facts f ON f.item_id = m.item_id
LEFT JOIN entities e ON e.entity_id = other.entity_id
WHERE {where} AND m.entity_id = $entity_id
  AND other.entity_id IS NOT NULL AND other.entity_id <> $entity_id
GROUP BY ALL
ORDER BY n DESC, entity
LIMIT 15
