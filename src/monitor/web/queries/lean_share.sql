-- Per lean bucket: items per storyline, and the bucket's relevant total.
WITH base AS (
    SELECT f.storyline_id, coalesce(s.lean_label, 'Unrated') AS lean_label
    FROM facts f LEFT JOIN sources s USING (source_id)
    WHERE {where}
),
totals AS (
    SELECT lean_label, count(*) AS bucket_total FROM base GROUP BY 1
)
SELECT b.storyline_id, b.lean_label, count(*) AS n, t.bucket_total
FROM base b JOIN totals t USING (lean_label)
WHERE b.storyline_id IN (SELECT unnest($ids))
GROUP BY ALL
UNION ALL
SELECT NULL, lean_label, 0, bucket_total FROM totals
