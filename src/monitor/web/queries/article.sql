-- One item with its outlet.
SELECT f.*,
       coalesce(s.name, f.source_id) AS outlet, s.lean_label, s.country,
       s.lean_source_url, s.lean_source, s.lean_retrieved_on,
       s.state_affiliated, s.notes
FROM facts f
LEFT JOIN sources s USING (source_id)
WHERE f.item_id = $item_id
