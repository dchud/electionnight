-- Storylines meeting the rising rule, by velocity: the last 15 minutes against
-- the prior 120. Outlets count every copy. Status is as of the time asked for.
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
           ) AS trailing
    FROM base
    GROUP BY 1
),
outlets AS (
    SELECT f.storyline_id, count(DISTINCT f.source_id) AS outlets
    FROM facts f
    WHERE {where_every_copy}
      AND f.storyline_id IS NOT NULL
      AND f.first_seen_at >= $as_of - INTERVAL 135 MINUTE
    GROUP BY 1
),
status_then AS (
    SELECT storyline_id, status FROM storyline_versions
    WHERE created_at <= $as_of
    QUALIFY row_number() OVER (
        PARTITION BY storyline_id ORDER BY version DESC, created_at DESC) = 1
),
scored AS (
    SELECT c.*,
           (c.observed - c.trailing * 15.0 / 120)
               / sqrt(c.trailing * 15.0 / 120 + 1) AS score
    FROM counts c
)
SELECT c.storyline_id, c.observed, c.trailing, o.outlets, s.label, s.summary,
       s.citations, s.first_seen_at, s.states, s.primary_level, c.score
FROM scored c
JOIN outlets o USING (storyline_id)
JOIN storylines s USING (storyline_id)
JOIN status_then t USING (storyline_id)
WHERE t.status = 'active' AND coalesce(s.label, '') <> ''
  AND c.score >= $min_score AND c.observed >= $min_observed
ORDER BY c.score DESC, c.observed DESC, s.storyline_id
LIMIT $limit
