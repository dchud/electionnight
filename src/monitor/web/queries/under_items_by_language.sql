-- Items per detected language.
SELECT language, count(*) AS items,
       count(*) FILTER (WHERE relevant) AS relevant,
       count(*) FILTER (WHERE relevant AND canonical) AS relevant_unique
FROM facts
GROUP BY 1
ORDER BY items DESC, language
