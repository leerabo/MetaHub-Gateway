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
CREATE FUNCTION ga_review_permission_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN RAISE EXCEPTION 'launch_review_permission_immutable'; END $$;
CREATE TRIGGER trg_ga_review_permission_immutable
BEFORE INSERT OR UPDATE OR DELETE ON platform_launch_review_role_permissions
FOR EACH ROW EXECUTE FUNCTION ga_review_permission_immutable();

ALTER TABLE post_ga_reviews DROP CONSTRAINT post_ga_reviews_ga_launch_id_key;
CREATE UNIQUE INDEX uq_post_ga_reviews_launch_conclusion
  ON post_ga_reviews(ga_launch_id, conclusion);
