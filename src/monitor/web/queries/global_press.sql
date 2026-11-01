-- Share of relevant coverage per non-English language, per 30 minutes.
WITH base AS (
    SELECT time_bucket(INTERVAL '30 minutes', f.first_seen_at) AS bin_start,
           f.language
    FROM facts f
    WHERE {where}
),
totals AS (SELECT bin_start, count(*) AS total FROM base GROUP BY 1)
SELECT b.bin_start, b.language, count(*) AS n, t.total,
       count(*) * 1.0 / t.total AS share
FROM base b JOIN totals t USING (bin_start)
WHERE b.language <> 'en'
GROUP BY b.bin_start, b.language, t.total
ORDER BY b.bin_start, b.language
