-- Entities by mentions in one storyline. Tone leaves out opinion items.
SELECT m.entity_id, coalesce(e.canonical_name, m.entity_id) AS entity,
       e.party, count(*) AS n,
       sum(m.tone) FILTER (WHERE NOT f.is_opinion) AS tone_sum,
       sum(m.tone * m.tone) FILTER (WHERE NOT f.is_opinion) AS tone_sq,
       count(m.tone) FILTER (WHERE NOT f.is_opinion) AS tone_n
FROM mentions m
JOIN facts f USING (item_id)
LEFT JOIN entities e USING (entity_id)
WHERE {where} AND f.storyline_id = $storyline_id AND m.entity_id IS NOT NULL
GROUP BY ALL
ORDER BY n DESC, entity
LIMIT 15
