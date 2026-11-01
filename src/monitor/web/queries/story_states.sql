-- Items per state in one storyline.
SELECT s AS usps, count(*) AS n
FROM (SELECT *, unnest(states) AS s FROM facts) f
WHERE {where} AND f.storyline_id = $storyline_id
GROUP BY 1
ORDER BY n DESC, usps
