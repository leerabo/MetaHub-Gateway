-- metahub:expected-version-guard-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE ga_review_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO ga_review_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0154' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_launch_role_permissions','post_ga_reviews'))=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE ga_review_up_guard;

CREATE TABLE platform_launch_review_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','release_manager')),
  permission_code TEXT NOT NULL CHECK(permission_code='platform.launch.review'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-148'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=1),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code)
);
INSERT INTO platform_launch_review_role_permissions VALUES
  ('platform_owner','platform.launch.review','PRD-148',1),
  ('release_manager','platform.launch.review','PRD-148',1);
CREATE TRIGGER trg_platform_launch_review_roles_insert_prd148
BEFORE INSERT ON platform_launch_review_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_review_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_review_roles_update_prd148
BEFORE UPDATE ON platform_launch_review_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_review_permission_immutable'); END;
CREATE TRIGGER trg_platform_launch_review_roles_delete_prd148
BEFORE DELETE ON platform_launch_review_role_permissions
BEGIN SELECT RAISE(ABORT,'launch_review_permission_immutable'); END;

DROP TRIGGER trg_post_ga_review_update_immutable;
DROP TRIGGER trg_post_ga_review_delete_immutable;
ALTER TABLE post_ga_reviews RENAME TO post_ga_reviews_0155_old;
CREATE TABLE post_ga_reviews(
  post_ga_review_id TEXT PRIMARY KEY CHECK(length(post_ga_review_id)=36
    AND substr(post_ga_review_id,1,4)='pgr_' AND substr(post_ga_review_id,5) NOT GLOB '*[^0-9a-f]*'),
  ga_launch_id TEXT NOT NULL REFERENCES ga_launches(ga_launch_id),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64 AND summary_sha256 NOT GLOB '*[^0-9a-f]*'),
  action_items_sha256 TEXT NOT NULL CHECK(length(action_items_sha256)=64 AND action_items_sha256 NOT GLOB '*[^0-9a-f]*'),
  conclusion TEXT NOT NULL CHECK(conclusion IN('ga_launched','ga_rolled_back')),
  reviewed_by_operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  reviewed_at TEXT NOT NULL CHECK(unixepoch(reviewed_at) IS NOT NULL),
  UNIQUE(ga_launch_id,conclusion)
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
