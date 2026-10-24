-- Password sessions have no MFA factor; existing enrolled sessions are retained.
ALTER TABLE platform_sessions ALTER COLUMN factor_id DROP NOT NULL;
ALTER TABLE platform_sessions ADD CONSTRAINT platform_sessions_optional_mfa_consistency
  CHECK ((factor_id IS NULL AND factor_revision=0)
      OR (factor_id IS NOT NULL AND factor_revision>=1));
