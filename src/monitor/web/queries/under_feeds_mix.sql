-- Every poll by outcome, all time and in the last hour.
SELECT CASE
           WHEN not_modified THEN '304 not modified'
           WHEN http_status = 200 AND error IS NOT NULL THEN '200 with no entries'
           WHEN http_status = 200 AND n_new > 0 THEN '200 with new items'
           WHEN http_status = 200 THEN '200 with nothing new'
           WHEN http_status IS NULL THEN 'connection error'
           ELSE 'HTTP ' || http_status
       END AS outcome,
       count(*) AS n,
       count(*) FILTER (WHERE started_at >= now() - INTERVAL 1 HOUR)
           AS last_hour,
       avg(duration_ms) AS mean_ms
FROM lake.raw.fetches
GROUP BY 1
ORDER BY n DESC
