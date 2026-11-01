-- Sizes per column, from one Parquet file's footer. No data page is read.
SELECT path_in_schema AS column_path, any_value(type) AS physical_type,
       any_value(compression) AS compression,
       any_value(encodings) AS encodings,
       sum(num_values) AS n_values,
       sum(total_compressed_size) AS compressed,
       sum(total_uncompressed_size) AS uncompressed
FROM parquet_metadata({path})
GROUP BY 1
ORDER BY compressed DESC
