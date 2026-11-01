-- Lake options as the catalog stores them. A scope of `table` is one table's
-- own setting.
SELECT key, value, scope, scope_id
FROM __ducklake_metadata_lake.main.ducklake_metadata
WHERE key IN (
    'version', 'parquet_compression', 'parquet_compression_level',
    'target_file_size', 'data_inlining_row_limit'
)
ORDER BY key, scope NULLS FIRST, scope_id
