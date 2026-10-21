-- Additive coordination metadata only. Apply 001, then 002 before this migration.
BEGIN;
-- Locate the exact D2 batch for an expired screening claim; legacy rows may be NULL.
ALTER TABLE reposeer.crawl_jobs ADD COLUMN IF NOT EXISTS screen_assignment_id uuid;
ALTER TABLE reposeer.repositories
  ADD COLUMN IF NOT EXISTS activity_coverage_status text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS activity_coverage_json jsonb,
  ADD COLUMN IF NOT EXISTS last_coverage_at timestamptz;
ALTER TABLE reposeer.packages
  ADD COLUMN IF NOT EXISTS last_screening_run_id uuid REFERENCES reposeer.crawl_runs(run_id),
  ADD COLUMN IF NOT EXISTS mapping_evidence jsonb,
  ADD COLUMN IF NOT EXISTS mapping_verified_at timestamptz;
ALTER TABLE reposeer.crawl_runs ADD COLUMN IF NOT EXISTS catalog_metadata jsonb;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='repositories_activity_coverage_status'
                 AND conrelid='reposeer.repositories'::regclass) THEN
    ALTER TABLE reposeer.repositories ADD CONSTRAINT repositories_activity_coverage_status
      CHECK (activity_coverage_status IN ('unknown','budget_limited','bounded_no_budget_exhaustion','collection_failed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='repositories_activity_coverage_object'
                 AND conrelid='reposeer.repositories'::regclass) THEN
    ALTER TABLE reposeer.repositories ADD CONSTRAINT repositories_activity_coverage_object
      CHECK (activity_coverage_json IS NULL OR
             (jsonb_typeof(activity_coverage_json)='object' AND octet_length(activity_coverage_json::text)<=65536));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='packages_mapping_evidence_object'
                 AND conrelid='reposeer.packages'::regclass) THEN
    ALTER TABLE reposeer.packages ADD CONSTRAINT packages_mapping_evidence_object
      CHECK (mapping_evidence IS NULL OR
             (jsonb_typeof(mapping_evidence)='object' AND octet_length(mapping_evidence::text)<=65536));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='crawl_runs_catalog_metadata_object'
                 AND conrelid='reposeer.crawl_runs'::regclass) THEN
    ALTER TABLE reposeer.crawl_runs ADD CONSTRAINT crawl_runs_catalog_metadata_object
      CHECK (catalog_metadata IS NULL OR
             (run_kind='integration' AND jsonb_typeof(catalog_metadata)='object'
              AND octet_length(catalog_metadata::text)<=65536));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS packages_pending_verified_repo_idx
  ON reposeer.packages(repository_id,package_id) WHERE mapping_status='candidate';
-- An accepted snapshot can be backfilled once for legacy rows, never revised.
CREATE OR REPLACE FUNCTION reposeer.keep_catalog_snapshot() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF OLD.catalog_metadata IS NOT NULL AND NEW.catalog_metadata IS DISTINCT FROM OLD.catalog_metadata THEN
    RAISE EXCEPTION 'accepted_catalog_snapshot_immutable';
  END IF;
  RETURN NEW;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='crawl_runs_catalog_immutable'
                 AND tgrelid='reposeer.crawl_runs'::regclass) THEN
    CREATE TRIGGER crawl_runs_catalog_immutable BEFORE UPDATE OF catalog_metadata
      ON reposeer.crawl_runs FOR EACH ROW EXECUTE FUNCTION reposeer.keep_catalog_snapshot();
  END IF;
END $$;
REVOKE ALL ON FUNCTION reposeer.keep_catalog_snapshot() FROM PUBLIC;
COMMIT;
