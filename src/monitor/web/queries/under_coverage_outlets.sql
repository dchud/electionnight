-- Every listed outlet, with its lean bucket and what it has delivered.
WITH delivered AS (
    SELECT source_id, count(*) AS items,
           count(*) FILTER (WHERE relevant) AS relevant,
           max(seen_raw_at) AS last_item
    FROM facts
    GROUP BY 1
)
SELECT s.source_id, s.name, s.scope, s.home_state, s.country, s.language,
       s.poll_tier, s.enabled, s.notes, s.paywall,
       CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                    AND coalesce(s.lean_source_url, '') <> ''
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' OR coalesce(s.country, 'US') <> 'US'
                   THEN 'Non-US'
               ELSE 'Unrated'
           END AS bucket,
       coalesce(d.items, 0) AS items,
       coalesce(d.relevant, 0) AS relevant,
       d.last_item
FROM sources s
LEFT JOIN delivered d USING (source_id)
ORDER BY s.name
