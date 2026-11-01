-- The newest maintenance and backup runs.
SELECT recorded_at, kind, tables_merged, files_removed, duration_ms,
       backup_bytes
FROM lake.nlp.maintenance_runs
ORDER BY recorded_at DESC
LIMIT 10
