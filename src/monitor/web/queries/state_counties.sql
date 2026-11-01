-- Items per county in one state, from gazetteer-matched places.
SELECT p.county_fips AS fips, count(DISTINCT p.item_id) AS n
FROM places p JOIN facts f USING (item_id)
WHERE {where} AND starts_with(p.county_fips, $fips)
GROUP BY 1
ORDER BY n DESC
