-- Which model answered, from the traces. Traces start when capture started.
SELECT purpose, model_requested, model_served, count(*) AS calls,
       min(created_at) AS first_trace, max(created_at) AS last_trace
FROM lake.nlp.llm_traces
GROUP BY ALL
ORDER BY calls DESC
