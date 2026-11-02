-- Race-call claims reported in news, grouped by race and candidate. Not
-- official. Once a race has a final claim, its leads and too-close reports
-- drop out, and every figure on a row describes the claims it shows. A
-- caller's name comes from article text, so the reporting outlets are shown
-- beside it.
WITH calls AS (
    SELECT rc.*, f.first_seen_at AS seen_at, f.source_id, f.item_id AS fact_id,
           rc.status IN ('projected', 'declared', 'conceded') AS final,
           coalesce(rc.race_key, rc.race_desc) AS race
    FROM race_calls rc
    JOIN facts f ON f.item_id = rc.item_id
    WHERE {where}
      AND rc.status IN ('projected', 'declared', 'conceded', 'leading',
                        'too_close')
    QUALIFY final OR NOT bool_or(final) OVER (
        PARTITION BY coalesce(rc.race_key, rc.race_desc)
    )
)
SELECT c.race,
       any_value(c.race_desc) AS race_desc,
       mode(c.winner_name) AS winner_name, any_value(c.party) AS party,
       arg_max(
           c.status,
           list_position(
               ['too_close', 'leading', 'projected', 'conceded', 'declared'],
               c.status
           )
       ) AS status,
       list(DISTINCT c.called_by) FILTER (WHERE c.called_by IS NOT NULL)
           AS called_by,
       list_sort(list(DISTINCT coalesce(s.name, c.source_id)))
           AS outlet_names,
       count(DISTINCT c.source_id) AS outlets,
       min(c.seen_at) AS first_reported,
       list(DISTINCT c.fact_id) AS item_ids,
       bool_or(c.final) AS final,
       count(*) OVER () AS total
FROM calls c
LEFT JOIN sources s ON s.source_id = c.source_id
GROUP BY c.race, coalesce(c.winner_entity_id, c.winner_name)
ORDER BY final DESC, first_reported DESC, c.race
LIMIT $limit
