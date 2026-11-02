-- Outlets whose relevant stories are mostly about a state other than their
-- home state, by the classifier's state tags. At least 5 tagged stories.
WITH tagged AS (
    SELECT source_id, unnest(states) AS state
    FROM facts WHERE relevant
),
counts AS (
    SELECT source_id, state, count(*) AS n FROM tagged GROUP BY 1, 2
),
ranked AS (
    SELECT *, row_number() OVER (
               PARTITION BY source_id ORDER BY n DESC, state) AS position,
           sum(n) OVER (PARTITION BY source_id) AS total
    FROM counts
)
SELECT s.source_id, s.name, s.home_state, r.state AS top_state,
       r.n AS top_state_stories, r.total AS tagged_stories,
       coalesce(h.n, 0) AS home_state_stories
FROM ranked r
JOIN sources s USING (source_id)
LEFT JOIN counts h
    ON h.source_id = r.source_id AND h.state = s.home_state
WHERE r.position = 1 AND r.total >= 5
  AND coalesce(s.home_state, '') <> '' AND r.state <> s.home_state
ORDER BY r.n DESC, s.name
