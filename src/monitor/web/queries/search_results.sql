-- Items by id, with what a search result row shows.
SELECT f.item_id, f.headline, f.title, f.summary, f.url, f.language,
       f.first_seen_at, f.published_at, f.primary_level, f.states, f.tone,
       f.topics, f.storyline_id, f.is_opinion,
       coalesce(s.name, f.source_id) AS outlet, f.source_id, s.lean_label,
       s.country, s.state_affiliated
FROM facts f LEFT JOIN sources s USING (source_id)
WHERE f.item_id IN (SELECT unnest($ids))
