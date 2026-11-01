-- Outlets, House seats, and stories per state. A story is a relevant, unique
-- item the classifier tagged with the state, so one item can count in several
-- states. House seats stand in for population.
WITH outlets AS (
    SELECT home_state AS usps,
           count(*) FILTER (WHERE enabled) AS enabled_outlets,
           count(*) AS listed_outlets,
           list(name ORDER BY name) FILTER (WHERE enabled) AS enabled_names,
           list(name ORDER BY name) FILTER (WHERE NOT enabled) AS no_feed_names
    FROM sources
    WHERE coalesce(home_state, '') <> ''
    GROUP BY 1
),
seats AS (
    SELECT state AS usps, count(*) AS house_seats
    FROM races WHERE office = 'house'
    GROUP BY 1
),
about AS (
    SELECT tagged AS usps, count(*) AS stories
    FROM (
        SELECT unnest(states) AS tagged FROM facts
        WHERE relevant AND canonical
          AND first_seen_at >= $start AND first_seen_at <= $end
    )
    GROUP BY 1
),
own AS (
    SELECT so.home_state AS usps, count(*) AS from_own_outlets
    FROM facts f JOIN sources so USING (source_id)
    WHERE f.relevant AND f.first_seen_at >= $start AND f.first_seen_at <= $end
      AND coalesce(so.home_state, '') <> ''
    GROUP BY 1
)
SELECT st.usps, st.name,
       list_contains($battleground, st.usps) AS battleground,
       coalesce(o.enabled_outlets, 0) AS enabled_outlets,
       coalesce(o.listed_outlets, 0) AS listed_outlets,
       o.enabled_names, o.no_feed_names,
       coalesce(se.house_seats, 0) AS house_seats,
       coalesce(a.stories, 0) AS stories,
       coalesce(ow.from_own_outlets, 0) AS from_own_outlets
FROM states st
LEFT JOIN outlets o USING (usps)
LEFT JOIN seats se USING (usps)
LEFT JOIN about a USING (usps)
LEFT JOIN own ow USING (usps)
ORDER BY st.usps
