-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE ga_review_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO ga_review_down_guard(issue_count)
SELECT (SELECT COUNT(*) FROM post_ga_reviews) -
       (SELECT COUNT(DISTINCT ga_launch_id) FROM post_ga_reviews);
DROP TABLE ga_review_down_guard;

DROP TRIGGER trg_platform_launch_review_roles_delete_prd148;
DROP TRIGGER trg_platform_launch_review_roles_update_prd148;
DROP TRIGGER trg_platform_launch_review_roles_insert_prd148;
DROP TABLE platform_launch_review_role_permissions;

DROP TRIGGER trg_post_ga_review_update_immutable;
DROP TRIGGER trg_post_ga_review_delete_immutable;
ALTER TABLE post_ga_reviews RENAME TO post_ga_reviews_0155_old;
CREATE TABLE post_ga_reviews(
  post_ga_review_id TEXT PRIMARY KEY CHECK(length(post_ga_review_id)=36
    AND substr(post_ga_review_id,1,4)='pgr_' AND substr(post_ga_review_id,5) NOT GLOB '*[^0-9a-f]*'),
  ga_launch_id TEXT NOT NULL UNIQUE REFERENCES ga_launches(ga_launch_id),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64 AND summary_sha256 NOT GLOB '*[^0-9a-f]*'),
  action_items_sha256 TEXT NOT NULL CHECK(length(action_items_sha256)=64 AND action_items_sha256 NOT GLOB '*[^0-9a-f]*'),
  conclusion TEXT NOT NULL CHECK(conclusion IN('ga_launched','ga_rolled_back')),
  reviewed_by_operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  reviewed_at TEXT NOT NULL CHECK(unixepoch(reviewed_at) IS NOT NULL)
);
INSERT INTO post_ga_reviews SELECT * FROM post_ga_reviews_0155_old;
DROP TABLE post_ga_reviews_0155_old;
CREATE TRIGGER trg_post_ga_review_update_immutable
BEFORE UPDATE ON post_ga_reviews
BEGIN SELECT RAISE(ABORT,'post_ga_review_immutable'); END;
CREATE TRIGGER trg_post_ga_review_delete_immutable
BEFORE DELETE ON post_ga_reviews
BEGIN SELECT RAISE(ABORT,'post_ga_review_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;
