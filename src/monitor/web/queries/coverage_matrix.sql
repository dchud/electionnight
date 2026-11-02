-- Items per storyline per lean bucket, and each bucket's relevant total.
WITH base AS (
    SELECT f.storyline_id, st.label,
           CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' THEN 'Non-US'
               ELSE 'Unrated'
           END AS bucket
    FROM facts f
    LEFT JOIN sources s USING (source_id)
    LEFT JOIN storylines st USING (storyline_id)
    WHERE {where}
),
totals AS (SELECT bucket, count(*) AS bucket_total FROM base GROUP BY 1)
SELECT b.storyline_id, b.label, b.bucket, count(*) AS n, t.bucket_total
FROM base b JOIN totals t USING (bucket)
WHERE b.storyline_id IS NOT NULL AND coalesce(b.label, '') <> ''
GROUP BY ALL
UNION ALL
SELECT NULL, NULL, bucket, 0, bucket_total FROM totals
