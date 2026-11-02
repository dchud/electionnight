-- Story totals for the window, and how many carry no state tag at all.
SELECT count(*) AS items,
       count(*) FILTER (WHERE relevant) AS relevant,
       count(*) FILTER (WHERE relevant AND canonical) AS relevant_unique,
       count(*) FILTER (WHERE relevant AND canonical AND len(states) = 0)
           AS no_state,
       (SELECT count(*) FROM facts WHERE relevant) AS relevant_all_time
FROM facts
WHERE first_seen_at >= $start AND first_seen_at <= $end
