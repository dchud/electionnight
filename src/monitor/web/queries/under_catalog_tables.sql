-- Rows, files, and bytes per table, from the catalog. No data file is opened.
-- Rows in the stats table that no live Parquet file holds are inlined rows.
WITH info AS (
    SELECT table_id, file_count, file_size_bytes, delete_file_count
    FROM ducklake_table_info('lake')
),
files AS (
    SELECT table_id, sum(record_count) AS parquet_rows
    FROM __ducklake_metadata_lake.main.ducklake_data_file
    WHERE end_snapshot IS NULL
    GROUP BY 1
)
SELECT s.schema_name, t.table_name, t.table_id,
       coalesce(st.record_count, 0) AS total_rows,
       coalesce(f.parquet_rows, 0) AS parquet_rows,
       coalesce(st.record_count, 0) - coalesce(f.parquet_rows, 0)
           AS inlined_rows,
       coalesce(i.file_count, 0) AS parquet_files,
       coalesce(i.file_size_bytes, 0) AS parquet_bytes,
       coalesce(i.delete_file_count, 0) AS delete_files
FROM __ducklake_metadata_lake.main.ducklake_table t
JOIN __ducklake_metadata_lake.main.ducklake_schema s USING (schema_id)
LEFT JOIN __ducklake_metadata_lake.main.ducklake_table_stats st
    USING (table_id)
LEFT JOIN files f USING (table_id)
LEFT JOIN info i USING (table_id)
WHERE t.end_snapshot IS NULL AND s.end_snapshot IS NULL
ORDER BY s.schema_name, t.table_name
