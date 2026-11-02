-- What conditional GET saved. A 304 sends no body, so its saving is estimated
-- as the same feed's mean 200 body. Sizes exist only since capture started.
WITH sizes AS (
    SELECT f.feed_url, avg(d.n_bytes) AS mean_bytes
    FROM lake.raw.fetches f
    JOIN lake.raw.fetch_details d USING (fetch_id)
    GROUP BY 1
),
polls AS (
    SELECT f.feed_url, f.not_modified, f.http_status, f.n_entries, f.n_new,
           s.mean_bytes, f.fetch_id
    FROM lake.raw.fetches f
    LEFT JOIN sizes s USING (feed_url)
)
SELECT count(*) AS polls,
       count(*) FILTER (WHERE not_modified) AS not_modified,
       count(*) FILTER (WHERE http_status = 200) AS full_responses,
       count(*) FILTER (WHERE not_modified AND mean_bytes IS NOT NULL)
           AS not_modified_sized,
       coalesce(sum(mean_bytes) FILTER (WHERE not_modified), 0)
           AS bytes_saved_est,
       (SELECT coalesce(sum(n_bytes), 0) FROM lake.raw.fetch_details)
           AS bytes_received,
       (SELECT count(*) FROM lake.raw.fetch_details WHERE unchanged)
           AS unchanged_bodies,
       (SELECT count(*) FROM lake.raw.fetch_details) AS detailed_responses,
       coalesce(sum(n_entries), 0) AS entries_served,
       coalesce(sum(n_new), 0) AS items_kept
FROM polls
