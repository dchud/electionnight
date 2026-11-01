-- Files a merge replaced, waiting for cleanup to delete them from disk.
SELECT count(*) AS n_files, min(schedule_start) AS oldest,
       max(schedule_start) AS newest
FROM __ducklake_metadata_lake.main.ducklake_files_scheduled_for_deletion
