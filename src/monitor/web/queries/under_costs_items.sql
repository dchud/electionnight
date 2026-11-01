-- What each stage produced, to divide spend by.
SELECT (SELECT count(*) FROM facts WHERE method = 'llm') AS classified_llm,
       (SELECT count(*) FROM facts WHERE tone IS NOT NULL) AS extracted,
       (SELECT count(*) FROM storylines WHERE coalesce(label, '') <> '')
           AS labeled,
       (SELECT count(*) FROM mentions) AS mentions,
       (SELECT count(*) FROM entities) AS entities,
       (SELECT count(*) FROM briefs) AS briefs
