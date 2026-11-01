-- Estimated spend in the ledger, in dollars.
SELECT coalesce(sum(usd_est), 0) AS usd FROM llm_calls
