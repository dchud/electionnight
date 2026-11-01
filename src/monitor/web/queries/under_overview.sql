-- Counts over the fact table: one row per item, in the web process's memory.
SELECT count(*) AS items,
       count(*) FILTER (WHERE relevant) AS relevant,
       count(*) FILTER (WHERE canonical) AS canonical,
       count(*) FILTER (WHERE relevant AND canonical) AS relevant_unique,
       count(*) FILTER (WHERE published_at < seen_raw_at - INTERVAL 2 HOUR)
           AS backlog,
       count(DISTINCT source_id) AS outlets_with_items,
       (SELECT count(*) FROM sources) AS outlets_listed,
       (SELECT count(*) FROM sources WHERE enabled) AS outlets_enabled,
       min(seen_raw_at) AS first_seen,
       max(seen_raw_at) AS last_seen
FROM facts
