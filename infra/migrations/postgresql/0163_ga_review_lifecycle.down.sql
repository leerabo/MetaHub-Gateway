-- The runner owns the transaction. Validate the old invariant before removing
-- any 0163 objects; two conclusions for one launch must abort the entire down.
ALTER TABLE post_ga_reviews ADD CONSTRAINT post_ga_reviews_ga_launch_id_key
  UNIQUE(ga_launch_id);
DROP INDEX uq_post_ga_reviews_launch_conclusion;
DROP TABLE platform_launch_review_role_permissions;
DROP FUNCTION ga_review_permission_immutable();
