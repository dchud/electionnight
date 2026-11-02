-- Spend per Eastern calendar day.
SELECT (created_at AT TIME ZONE 'America/New_York')::DATE AS day,
       count(*) AS calls, coalesce(sum(usd_est), 0) AS usd
FROM llm_calls
GROUP BY 1
ORDER BY 1
