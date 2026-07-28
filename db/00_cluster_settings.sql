-- Run ONCE against the cluster, as an admin, before applying schema.sql.
--
-- Vector indexes are gated behind a cluster setting. Without this, every
-- `VECTOR INDEX` clause in schema.sql fails.
SET CLUSTER SETTING feature.vector_index.enabled = true;

-- Only needed when adding a vector index to an already-populated table.
-- Not required for a fresh schema apply.
--   SET sql_safe_updates = false;
