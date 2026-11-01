-- Each system prompt, with how many rows and traces carry it. A prompt is
-- written once per pipeline start, so rows exceed distinct prompts.
WITH used AS (
    SELECT prompt_hash, count(*) AS traces, max(created_at) AS last_used
    FROM lake.nlp.llm_traces
    GROUP BY 1
)
SELECT p.purpose, p.prompt_hash, any_value(p.text) AS text,
       count(*) AS rows_written, min(p.created_at) AS first_written,
       any_value(u.traces) AS traces, any_value(u.last_used) AS last_used
FROM lake.nlp.llm_prompts p
LEFT JOIN used u USING (prompt_hash)
GROUP BY p.purpose, p.prompt_hash
ORDER BY p.purpose, last_used DESC NULLS LAST
