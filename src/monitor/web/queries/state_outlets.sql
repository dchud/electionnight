-- Outlets based in one state, with their relevant item count in the window.
SELECT s.source_id, s.name AS outlet, s.lean_label, s.country, s.scope,
       s.enabled, count(f.item_id) AS n
FROM sources s
LEFT JOIN facts f ON f.source_id = s.source_id AND {where}
WHERE s.home_state = $usps
GROUP BY ALL
ORDER BY n DESC, outlet
