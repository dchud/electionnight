-- 5-minute bins for the top 10 storylines in the window, plus Other. Every
-- series has a row in every bin, so areas drop to zero between stories.
WITH base AS (
    SELECT f.storyline_id,
           time_bucket(INTERVAL '5 minutes', f.first_seen_at) AS bin_start
    FROM facts f
    WHERE {where} AND f.storyline_id IS NOT NULL
),
top_lines AS (
    SELECT b.storyline_id, s.label, count(*) AS total
    FROM base b JOIN storylines s USING (storyline_id)
    WHERE coalesce(s.label, '') <> ''
    GROUP BY 1, 2
    ORDER BY total DESC, b.storyline_id
    LIMIT 10
),
labeled AS (
    SELECT b.bin_start,
           coalesce(t.storyline_id, 'other') AS storyline_id,
           coalesce(t.label, 'Other') AS label
    FROM base b LEFT JOIN top_lines t USING (storyline_id)
),
series AS (
    SELECT storyline_id, any_value(label) AS label, count(*) AS total
    FROM labeled GROUP BY 1
),
bins AS (
    SELECT unnest(generate_series(
        time_bucket(INTERVAL '5 minutes', $window_start::TIMESTAMPTZ),
        time_bucket(INTERVAL '5 minutes', $as_of::TIMESTAMPTZ),
        INTERVAL '5 minutes'
    )) AS bin_start
),
counts AS (
    SELECT bin_start, storyline_id, count(*) AS n FROM labeled GROUP BY 1, 2
)
SELECT b.bin_start, s.storyline_id, s.label, s.total, coalesce(c.n, 0) AS n
FROM bins b
CROSS JOIN series s
LEFT JOIN counts c USING (bin_start, storyline_id)
ORDER BY b.bin_start, s.total DESC
