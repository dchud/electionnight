-- Spend per hour, newest 24 hours with any spend.
SELECT time_bucket(INTERVAL 1 HOUR, created_at) AS hour,
       count(*) AS calls, coalesce(sum(usd_est), 0) AS usd
FROM llm_calls
GROUP BY 1
ORDER BY 1 DESC
LIMIT 24
