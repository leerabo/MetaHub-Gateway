-- PRD-148：为 ModelAlias 增加有序、多候选备用模型集合。
CREATE TABLE IF NOT EXISTS "model_alias_fallback_candidates" (
  "model_alias_fallback_candidate_id" TEXT PRIMARY KEY,
  "tenant_id" TEXT NOT NULL,
  "model_alias_id" TEXT NOT NULL,
  "model_id" TEXT NOT NULL,
  "configured_priority" INTEGER NOT NULL CHECK ("configured_priority" BETWEEN 1 AND 32),
  "created_at" TEXT NOT NULL,
  "updated_at" TEXT NOT NULL,
  CONSTRAINT "fk_model_alias_fallback_candidates_tenant"
    FOREIGN KEY ("tenant_id") REFERENCES "tenants"("tenant_id"),
  CONSTRAINT "fk_model_alias_fallback_candidates_alias"
    FOREIGN KEY ("tenant_id", "model_alias_id")
    REFERENCES "model_aliases"("tenant_id", "model_alias_id"),
  CONSTRAINT "fk_model_alias_fallback_candidates_model"
    FOREIGN KEY ("tenant_id", "model_id")
    REFERENCES "models"("tenant_id", "model_id"),
  CONSTRAINT "uq_model_alias_fallback_candidates_priority"
    UNIQUE ("tenant_id", "model_alias_id", "configured_priority"),
  CONSTRAINT "uq_model_alias_fallback_candidates_model"
    UNIQUE ("tenant_id", "model_alias_id", "model_id")
);

CREATE INDEX IF NOT EXISTS "idx_model_alias_fallback_candidates_alias"
  ON "model_alias_fallback_candidates" ("tenant_id", "model_alias_id", "configured_priority");

INSERT INTO "model_alias_fallback_candidates"(
  "model_alias_fallback_candidate_id", "tenant_id", "model_alias_id", "model_id",
  "configured_priority", "created_at", "updated_at"
)
SELECT
  'mafc_' || replace(gen_random_uuid()::text, '-', ''),
  "tenant_id", "model_alias_id", "fallback_model_id", 1, "updated_at", "updated_at"
FROM "model_aliases"
WHERE "fallback_model_id" IS NOT NULL
ON CONFLICT ("tenant_id", "model_alias_id", "model_id") DO NOTHING;
