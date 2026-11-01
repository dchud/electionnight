-- Tone toward one entity per bin. Opinion items are excluded.
SELECT time_bucket(INTERVAL '{bin} minutes', f.first_seen_at) AS bin_start,
       sum(m.tone) AS tone_sum, sum(m.tone * m.tone) AS tone_sq,
       count(m.tone) AS tone_n
FROM mentions m JOIN facts f USING (item_id)
WHERE {where} AND m.entity_id = $entity_id AND NOT f.is_opinion
GROUP BY 1
ORDER BY 1
