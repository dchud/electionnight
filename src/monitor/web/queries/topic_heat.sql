-- The 14 busiest topics by 15-minute bin over the last 3 hours.
WITH base AS (
    SELECT time_bucket(INTERVAL '15 minutes', f.first_seen_at) AS bin_start,
           topic
    FROM (SELECT *, unnest(topics) AS topic FROM facts) f
    WHERE {where} AND f.first_seen_at >= $since
),
top_topics AS (
    SELECT topic, count(*) AS total FROM base
    GROUP BY 1 ORDER BY total DESC, topic LIMIT 14
)
SELECT b.bin_start, b.bin_start + INTERVAL 15 MINUTE AS bin_end, b.topic,
       t.total, count(*) AS n
FROM base b JOIN top_topics t USING (topic)
GROUP BY ALL
ORDER BY b.bin_start, t.total DESC
