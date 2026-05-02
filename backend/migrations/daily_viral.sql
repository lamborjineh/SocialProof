-- ─────────────────────────────────────────────────────────────────────────────
-- migrations/daily_viral.sql  (age-engagement features removed)
-- NOTE: This file is not needed if you are not deploying the Daily Viral
-- feature. The daily_viral router has been removed from main.py.
-- If you do need the corpus pool_tag columns for other purposes, run only
-- statements 1–2 below and skip statements 3 onward.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Add pool_tag + daily_date columns to existing corpus table
--    pool_tag no longer carries age-cohort values (kids_safe / older_curated removed)
ALTER TABLE corpus
    ADD COLUMN pool_tag   VARCHAR(30) NOT NULL DEFAULT 'general'
        COMMENT 'general | curated'
        AFTER source_domain,
    ADD COLUMN daily_date DATE NULL DEFAULT NULL
        COMMENT 'Set by admin cron to schedule this item as the daily pick'
        AFTER pool_tag;

CREATE INDEX idx_corpus_pool_date ON corpus (pool_tag, daily_date, is_active);

-- 2. Seed: label existing corpus rows to the general pool
UPDATE corpus SET pool_tag = 'general' WHERE pool_tag = '';

-- ─────────────────────────────────────────────────────────────────────────────
-- The following tables/columns were part of the age-engagement feature
-- and are NOT created by this migration:
--   - daily_evaluations  (age_group column removed; table not created)
--   - daily_comments     (teen-only feature; table not created)
--   - quiz_questions.age_groups column  (age-cohort filtering removed)
--   - users.age_group column            (removed from ORM and auth router)
--   - evaluations.age_group column      (removed from ORM and schemas)
-- ─────────────────────────────────────────────────────────────────────────────
