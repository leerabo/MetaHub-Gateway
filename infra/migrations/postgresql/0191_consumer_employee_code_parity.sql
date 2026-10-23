-- The PostgreSQL consumer table was introduced after 0154 and missed its
-- employee-code column. Preserve existing directory rows and add the contract.
ALTER TABLE workspace_consumers ADD COLUMN IF NOT EXISTS employee_code TEXT
  NOT NULL DEFAULT '' CHECK(length(employee_code)<=80);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_consumers_employee_code
  ON workspace_consumers(tenant_id,employee_code) WHERE employee_code!='';
