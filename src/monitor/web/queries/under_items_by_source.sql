-- Items per outlet. `source_id` is the first feed to serve an item.
SELECT f.source_id, coalesce(s.name, f.source_id) AS outlet, count(*) AS items,
       count(*) FILTER (WHERE f.relevant) AS relevant,
       count(*) FILTER (WHERE f.canonical) AS canonical,
       count(*) FILTER (
           WHERE f.published_at < f.seen_raw_at - INTERVAL 2 HOUR
       ) AS backlog
FROM facts f
LEFT JOIN sources s USING (source_id)
GROUP BY ALL
ORDER BY items DESC, f.source_id
LIMIT 25
