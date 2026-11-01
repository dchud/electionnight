-- Race-call claims reported in news, grouped by race and winner. Not official.
WITH calls AS (
    SELECT rc.*, f.first_seen_at AS seen_at, f.source_id, f.item_id AS fact_id
    FROM race_calls rc
    JOIN facts f ON f.item_id = rc.item_id
    WHERE {where}
      AND rc.status IN ('projected', 'declared', 'conceded', 'leading',
                        'too_close')
)
SELECT coalesce(race_key, race_desc) AS race,
       any_value(race_desc) AS race_desc,
       winner_name, any_value(party) AS party,
       mode(status) AS status,
       list(DISTINCT called_by) FILTER (WHERE called_by IS NOT NULL)
           AS called_by,
       count(DISTINCT source_id) AS outlets,
       min(seen_at) AS first_reported,
       list(DISTINCT fact_id) AS item_ids
FROM calls
GROUP BY coalesce(race_key, race_desc), winner_name
ORDER BY first_reported DESC
LIMIT $limit
