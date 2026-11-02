-- The newest relevant items from each outlet based in one state.
SELECT f.item_id, f.headline, f.title, f.url, f.language, f.first_seen_at,
       f.published_at, f.primary_level, f.states, f.tone, f.topics,
       f.storyline_id, f.is_opinion,
       coalesce(s.name, f.source_id) AS outlet, f.source_id, s.lean_label,
       s.country, s.state_affiliated
FROM facts f JOIN sources s USING (source_id)
WHERE {where} AND s.home_state = $usps
QUALIFY row_number() OVER (
    PARTITION BY f.source_id ORDER BY f.first_seen_at DESC, f.item_id
) <= $per_outlet
ORDER BY f.first_seen_at DESC, f.item_id
