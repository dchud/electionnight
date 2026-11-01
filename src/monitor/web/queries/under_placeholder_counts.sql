-- Stored values that do not mean what they appear to mean.
SELECT
    (SELECT count(*) FROM lake.nlp.dup_groups WHERE method = 'url')
        AS dup_url,
    (SELECT count(*) FROM lake.nlp.dup_groups WHERE method = 'none')
        AS dup_none,
    (SELECT max(created_at) FROM lake.nlp.dup_groups WHERE method = 'url')
        AS dup_url_last,
    (SELECT count(*) FROM lake.nlp.storyline_assign WHERE similarity = 1.0)
        AS similarity_one,
    (SELECT count(*) FROM lake.nlp.storyline_assign) AS assign_rows,
    (SELECT count(*) FROM lake.nlp.cluster_decisions
        WHERE stage = 'storylines') AS storyline_decisions
