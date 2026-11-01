-- The physical schema in one Parquet file's footer.
SELECT name, type, repetition_type, num_children, converted_type, duckdb_type
FROM parquet_schema({path})
