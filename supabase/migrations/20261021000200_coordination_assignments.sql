-- Additive only: apply on the coordinator, never automatically from these tools.
BEGIN;
ALTER TABLE reposeer.crawl_jobs
  ADD COLUMN IF NOT EXISTS assignment_targets jsonb,
  ADD COLUMN IF NOT EXISTS assignment_sha256 text;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'crawl_jobs_assignment_sha256_format'
                   AND conrelid = 'reposeer.crawl_jobs'::regclass) THEN
    ALTER TABLE reposeer.crawl_jobs ADD CONSTRAINT crawl_jobs_assignment_sha256_format
      CHECK (assignment_sha256 IS NULL OR assignment_sha256 ~ '^[a-f0-9]{64}$');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'crawl_jobs_assignment_targets_array'
                   AND conrelid = 'reposeer.crawl_jobs'::regclass) THEN
    ALTER TABLE reposeer.crawl_jobs ADD CONSTRAINT crawl_jobs_assignment_targets_array
      CHECK (assignment_targets IS NULL OR
        CASE WHEN jsonb_typeof(assignment_targets) = 'array'
             THEN jsonb_array_length(assignment_targets) BETWEEN 1 AND 100 ELSE false END);
  END IF;
END $$;
COMMIT;
