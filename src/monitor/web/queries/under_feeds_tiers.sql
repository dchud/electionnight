-- Yield per polling tier: new items per 100 polls.
WITH sources AS (
    SELECT source_id, poll_tier
    FROM lake.ref.sources
    QUALIFY row_number() OVER (
        PARTITION BY source_id ORDER BY created_at DESC) = 1
)
SELECT coalesce(s.poll_tier, 0) AS tier,
       count(DISTINCT f.feed_url) AS feeds,
       count(*) AS polls,
       coalesce(sum(f.n_entries), 0) AS entries_served,
       coalesce(sum(f.n_new), 0) AS items_kept,
       coalesce(sum(f.n_new), 0) * 100.0 / count(*) AS new_per_100_polls,
       count(*) FILTER (WHERE f.error IS NOT NULL) AS errors
FROM lake.raw.fetches f
LEFT JOIN sources s USING (source_id)
GROUP BY 1
ORDER BY 1
