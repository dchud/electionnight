-- Storylines with items about one state.
SELECT f.storyline_id, st.label, st.status, count(*) AS n,
       min(f.first_seen_at) AS first_seen_at
FROM facts f
JOIN storylines st ON st.storyline_id = f.storyline_id
WHERE {where} AND list_contains(f.states, $usps)
  AND coalesce(st.label, '') <> ''
GROUP BY ALL
ORDER BY n DESC, st.label
LIMIT 20
