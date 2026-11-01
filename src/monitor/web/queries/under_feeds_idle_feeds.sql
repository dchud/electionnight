-- Feeds polled repeatedly that have never added an item.
SELECT feed_url, any_value(source_id) AS source_id, count(*) AS polls,
       coalesce(sum(n_entries), 0) AS entries_served,
       max(started_at) AS last_poll
FROM lake.raw.fetches
GROUP BY 1
HAVING coalesce(sum(n_new), 0) = 0
ORDER BY polls DESC, feed_url
LIMIT 20
