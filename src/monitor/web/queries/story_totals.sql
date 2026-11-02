-- Totals for one storyline: unique, all copies, and all time.
SELECT count(*) AS n, count(DISTINCT f.source_id) AS outlets
FROM facts f
WHERE {where} AND f.storyline_id = $storyline_id
