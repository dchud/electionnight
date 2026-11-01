-- Items in one storyline, one row per duplicate group, with the group's size.
WITH members AS (
    SELECT f.item_id, f.headline, f.title, f.url, f.language, f.first_seen_at,
           f.published_at, f.primary_level, f.states, f.tone, f.topics,
           f.storyline_id, f.is_opinion, f.canonical, f.dup_group_id,
           coalesce(s.name, f.source_id) AS outlet, f.source_id, s.lean_label,
           s.country, s.state_affiliated
    FROM facts f LEFT JOIN sources s USING (source_id)
    WHERE {where} AND f.storyline_id = $storyline_id AND {filter}
),
sizes AS (
    SELECT dup_group_id, count(*) AS copies FROM facts
    WHERE first_seen_at <= $as_of
      AND dup_group_id IN (SELECT dup_group_id FROM members)
    GROUP BY 1
),
shown AS (
    SELECT * FROM members
    QUALIFY row_number() OVER (
        PARTITION BY {group_key}
        ORDER BY canonical DESC, first_seen_at, item_id
    ) = 1
)
SELECT shown.*, coalesce(sizes.copies, 1) AS copies, count(*) OVER () AS total
FROM shown LEFT JOIN sizes USING (dup_group_id)
ORDER BY first_seen_at DESC, item_id
LIMIT $limit
