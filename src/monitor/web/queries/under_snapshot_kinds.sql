-- What every snapshot did, by kind of change. One snapshot can change many
-- tables, so these count tables touched, not snapshots.
WITH changes AS (
    SELECT snapshot_id,
           unnest(string_split(changes_made, ',')) AS change
    FROM __ducklake_metadata_lake.main.ducklake_snapshot_changes
)
SELECT regexp_extract(change, '^([a-z_]+)', 1) AS kind,
       count(*) AS table_changes,
       count(DISTINCT snapshot_id) AS snapshots,
       max(snapshot_id) AS last_snapshot
FROM changes
GROUP BY 1
ORDER BY table_changes DESC
