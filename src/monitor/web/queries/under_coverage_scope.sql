-- Lean ratings by the kind of outlet. Enabled outlets only.
SELECT s.scope,
       count(*) AS outlets,
       count(*) FILTER (
           WHERE CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                    AND coalesce(s.lean_source_url, '') <> ''
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' OR coalesce(s.country, 'US') <> 'US'
                   THEN 'Non-US'
               ELSE 'Unrated'
           END
               NOT IN ('Unrated', 'Non-US')
       ) AS rated,
       count(*) FILTER (WHERE CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                    AND coalesce(s.lean_source_url, '') <> ''
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' OR coalesce(s.country, 'US') <> 'US'
                   THEN 'Non-US'
               ELSE 'Unrated'
           END = 'Unrated') AS unrated,
       count(*) FILTER (WHERE CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                    AND coalesce(s.lean_source_url, '') <> ''
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' OR coalesce(s.country, 'US') <> 'US'
                   THEN 'Non-US'
               ELSE 'Unrated'
           END = 'Non-US') AS non_us
FROM sources s
WHERE s.enabled
GROUP BY 1
ORDER BY outlets DESC
