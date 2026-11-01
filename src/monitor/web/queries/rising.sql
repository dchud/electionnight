-- Storylines ranked by velocity: the last 15 minutes against the prior 120.
WITH base AS (
    SELECT f.storyline_id, f.first_seen_at, f.source_id
    FROM facts f
    WHERE {where}
      AND f.storyline_id IS NOT NULL
      AND f.first_seen_at >= $as_of - INTERVAL 135 MINUTE
),
counts AS (
    SELECT storyline_id,
           count(*) FILTER (
               WHERE first_seen_at >= $as_of - INTERVAL 15 MINUTE
           ) AS observed,
           count(*) FILTER (
               WHERE first_seen_at < $as_of - INTERVAL 15 MINUTE
           ) AS trailing,
           count(DISTINCT source_id) AS outlets
    FROM base
    GROUP BY 1
)
SELECT c.storyline_id, c.observed, c.trailing, c.outlets, s.label, s.summary,
       s.citations, s.first_seen_at, s.states, s.primary_level,
       (c.observed - c.trailing * 15.0 / 120)
           / sqrt(c.trailing * 15.0 / 120 + 1) AS score
FROM counts c
JOIN storylines s USING (storyline_id)
WHERE s.status = 'active' AND coalesce(s.label, '') <> ''
ORDER BY score DESC, c.observed DESC, s.storyline_id
LIMIT $limit
