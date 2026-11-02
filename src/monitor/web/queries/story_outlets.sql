-- Outlets covering one storyline, earliest first. Counts all copies.
SELECT f.source_id, coalesce(s.name, f.source_id) AS outlet, s.lean_label,
       s.country, count(*) AS n, min(f.first_seen_at) AS first_report
FROM facts f LEFT JOIN sources s USING (source_id)
WHERE {where} AND f.storyline_id = $storyline_id
GROUP BY ALL
ORDER BY first_report, outlet
LIMIT 200
