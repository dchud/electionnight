-- LLM calls per purpose and model.
SELECT purpose, model, count(*) AS calls,
       coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(cache_read_tokens), 0) AS cache_read_tokens,
       coalesce(sum(cache_write_tokens), 0) AS cache_write_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(usd_est), 0) AS usd,
       count(*) FILTER (WHERE status <> 'ok') AS not_ok,
       avg(latency_ms) AS latency_ms
FROM llm_calls
WHERE created_at <= $as_of
GROUP BY 1, 2
ORDER BY usd DESC, purpose, model
