-- Outlets and stories per language. Language here is the language detected on
-- each item, so an outlet can publish in more than its listed language.
WITH outlets AS (
    SELECT language, count(*) FILTER (WHERE enabled) AS enabled_outlets,
           count(*) AS listed_outlets,
           list(name ORDER BY name) FILTER (WHERE NOT enabled) AS no_feed_names
    FROM sources
    GROUP BY 1
),
stories AS (
    SELECT language, count(*) AS items,
           count(*) FILTER (WHERE relevant) AS relevant,
           count(*) FILTER (WHERE relevant AND canonical) AS relevant_unique
    FROM facts
    WHERE first_seen_at >= $start AND first_seen_at <= $end
    GROUP BY 1
)
SELECT coalesce(o.language, st.language) AS language,
       coalesce(o.enabled_outlets, 0) AS enabled_outlets,
       coalesce(o.listed_outlets, 0) AS listed_outlets,
       o.no_feed_names,
       coalesce(st.items, 0) AS items,
       coalesce(st.relevant, 0) AS relevant,
       coalesce(st.relevant_unique, 0) AS relevant_unique
FROM outlets o
FULL JOIN stories st USING (language)
ORDER BY enabled_outlets DESC, relevant DESC
