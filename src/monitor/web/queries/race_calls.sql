-- Race-call claims reported in news, grouped by race and winner. Not official.
-- A caller's name comes from article text, so the reporting outlets are shown
-- beside it.
WITH calls AS (
    SELECT rc.*, f.first_seen_at AS seen_at, f.source_id, f.item_id AS fact_id
    FROM race_calls rc
    JOIN facts f ON f.item_id = rc.item_id
    WHERE {where}
      AND rc.status IN ('projected', 'declared', 'conceded', 'leading',
                        'too_close')
)
SELECT coalesce(c.race_key, c.race_desc) AS race,
       any_value(c.race_desc) AS race_desc,
       mode(c.winner_name) AS winner_name, any_value(c.party) AS party,
       mode(c.status) AS status,
       list(DISTINCT c.called_by) FILTER (WHERE c.called_by IS NOT NULL)
           AS called_by,
       list_sort(list(DISTINCT coalesce(s.name, c.source_id)))
           AS outlet_names,
       count(DISTINCT c.source_id) AS outlets,
       min(c.seen_at) AS first_reported,
       list(DISTINCT c.fact_id) AS item_ids
FROM calls c
LEFT JOIN sources s ON s.source_id = c.source_id
GROUP BY coalesce(c.race_key, c.race_desc),
         coalesce(c.winner_entity_id, c.winner_name)
ORDER BY first_reported DESC
LIMIT $limit
