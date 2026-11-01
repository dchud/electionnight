-- Outlets and stories per lean bucket. Stories count all copies, because
-- republishing is an outlet's coverage choice. A rating with no citation
-- counts as Unrated, the same rule the Sources page uses.
WITH labeled AS (
    SELECT s.source_id, s.name, s.enabled, s.scope,
           CASE
               WHEN s.lean_label IN ('L', 'LL', 'C', 'LR', 'R')
                    AND coalesce(s.lean_source_url, '') <> ''
                   THEN s.lean_label
               WHEN s.lean_label = 'NA' OR coalesce(s.country, 'US') <> 'US'
                   THEN 'Non-US'
               ELSE 'Unrated'
           END AS bucket
    FROM sources s
),
stories AS (
    SELECT l.bucket, f.source_id, count(*) AS stories,
           count(*) FILTER (WHERE f.tone IS NOT NULL) AS with_tone
    FROM facts f
    JOIN labeled l USING (source_id)
    WHERE f.relevant AND f.first_seen_at >= $start AND f.first_seen_at <= $end
    GROUP BY 1, 2
),
buckets AS (
    SELECT bucket, sum(stories) AS stories, sum(with_tone) AS with_tone,
           max(stories) AS top_outlet_stories,
           count(*) AS outlets_with_stories
    FROM stories
    GROUP BY 1
),
listed AS (
    SELECT bucket, count(*) FILTER (WHERE enabled) AS enabled_outlets,
           count(*) AS listed_outlets,
           list(name ORDER BY name) FILTER (WHERE enabled) AS names
    FROM labeled
    GROUP BY 1
)
SELECT l.bucket, l.enabled_outlets, l.listed_outlets, l.names,
       coalesce(b.stories, 0) AS stories,
       coalesce(b.with_tone, 0) AS with_tone,
       coalesce(b.top_outlet_stories, 0) AS top_outlet_stories,
       coalesce(b.outlets_with_stories, 0) AS outlets_with_stories,
       (SELECT l2.name FROM stories st JOIN labeled l2 USING (source_id)
        WHERE st.bucket = l.bucket
        ORDER BY st.stories DESC, st.source_id LIMIT 1) AS top_outlet
FROM listed l
LEFT JOIN buckets b USING (bucket)
