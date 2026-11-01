-- Rank of the top 12 entities by mentions per 15-minute bin.
WITH base AS (
    SELECT m.entity_id,
           time_bucket(INTERVAL '15 minutes', f.first_seen_at) AS bin_start,
           count(*) AS n
    FROM mentions m JOIN facts f USING (item_id)
    WHERE {where} AND m.entity_id IS NOT NULL
    GROUP BY ALL
),
top_entities AS (
    SELECT entity_id, sum(n) AS total FROM base
    GROUP BY 1 ORDER BY total DESC, entity_id LIMIT 12
),
ranked AS (
    SELECT b.*, rank() OVER (
        PARTITION BY b.bin_start ORDER BY b.n DESC, b.entity_id
    ) AS rank
    FROM base b WHERE entity_id IN (SELECT entity_id FROM top_entities)
)
SELECT r.bin_start, r.entity_id, r.n, r.rank,
       coalesce(e.canonical_name, r.entity_id) AS entity,
       coalesce(e.party, 'Other') AS party
FROM ranked r LEFT JOIN entities e USING (entity_id)
ORDER BY r.bin_start, r.rank
