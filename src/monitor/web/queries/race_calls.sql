-- Race-call claims reported in news, grouped by race and candidate. Not
-- official. A race is settled once a final claim names a caller or comes from
-- two outlets. Only the time asked for decides that, so a filter that hides
-- the final claim cannot bring back a lead. A settled race shows only its
-- final claims. Every figure on a row describes the claims on that row. A
-- caller's name comes from article text, so the outlets are shown beside it.
WITH settled AS (
    SELECT coalesce(rc.race_key, rc.race_desc) AS race
    FROM race_calls rc
    JOIN facts f ON f.item_id = rc.item_id
    WHERE {when} AND rc.status IN ('projected', 'declared', 'conceded')
    GROUP BY 1
    HAVING count(DISTINCT f.source_id) >= 2
        OR bool_or(
            coalesce(rc.called_by, 'unspecified')
                NOT IN ('unspecified', 'other')
        )
),
calls AS (
    SELECT rc.*, f.first_seen_at AS seen_at, f.source_id, f.item_id AS fact_id,
           rc.status IN ('projected', 'declared', 'conceded') AS final,
           coalesce(rc.race_key, rc.race_desc) AS race,
           CASE
               WHEN rc.status = 'too_close' THEN NULL
               ELSE coalesce(rc.winner_entity_id, rc.winner_name)
           END AS candidate
    FROM race_calls rc
    JOIN facts f ON f.item_id = rc.item_id
    WHERE {where}
      AND rc.status IN ('projected', 'declared', 'conceded', 'leading',
                        'too_close')
)
SELECT c.race,
       any_value(c.race_desc) AS race_desc,
       mode(c.winner_name) FILTER (WHERE c.candidate IS NOT NULL)
           AS winner_name,
       any_value(c.party) FILTER (WHERE c.candidate IS NOT NULL) AS party,
       list_sort(list(DISTINCT c.status)) AS statuses,
       list(DISTINCT c.called_by) FILTER (WHERE c.called_by IS NOT NULL)
           AS called_by,
       list_sort(list(DISTINCT coalesce(s.name, c.source_id)))
           AS outlet_names,
       count(DISTINCT c.source_id) AS outlets,
       min(c.seen_at) AS first_reported,
       list(DISTINCT c.fact_id) AS item_ids,
       count(*) OVER () AS total
FROM calls c
LEFT JOIN sources s ON s.source_id = c.source_id
WHERE c.final OR c.race NOT IN (SELECT race FROM settled)
GROUP BY c.race, c.final, c.candidate
ORDER BY first_reported DESC, c.race
LIMIT $limit
