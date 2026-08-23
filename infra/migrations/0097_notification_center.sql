-- Build the Workspace notification center, immutable template versions and asynchronous delivery facts.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd105_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd105_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0096' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','workspaces','workspace_tenant_bindings','workspace_memberships',
      'platform_roles','platform_operators','platform_operator_role_bindings',
      'platform_audit_logs'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd105')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd105_up_guard;

-- The original role catalog is immutable. Rebuild it transactionally so existing bindings remain
-- valid while adding the dedicated communications role required for template publication.
DROP TRIGGER trg_platform_roles_delete_prd095;
DROP TRIGGER trg_platform_roles_update_prd095;
DROP TRIGGER trg_platform_roles_insert_prd095;
ALTER TABLE platform_roles RENAME TO platform_roles_prd105;
CREATE TABLE platform_roles(
  role_code TEXT PRIMARY KEY CHECK(role_code IN(
    'platform_owner','platform_ops','platform_security','platform_billing',
    'platform_support','platform_verification','platform_compliance','platform_privacy',
    'release_manager','platform_provider_engineer','platform_channel',
    'platform_customer_success','platform_growth','platform_communications')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision IN(1,7))
);
INSERT INTO platform_roles(role_code,catalog_revision)
SELECT role_code,catalog_revision FROM platform_roles_prd105;
INSERT INTO platform_roles(role_code,catalog_revision) VALUES('platform_communications',7);
DROP TABLE platform_roles_prd105;
CREATE TRIGGER trg_platform_roles_insert_prd095 BEFORE INSERT ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_update_prd095 BEFORE UPDATE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;
CREATE TRIGGER trg_platform_roles_delete_prd095 BEFORE DELETE ON platform_roles
BEGIN SELECT RAISE(ABORT,'platform_catalog_immutable'); END;

CREATE TABLE platform_notification_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.notification_template.read','platform.notification_template.write')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-105'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=7)
);
CREATE TABLE platform_notification_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_notification_permission_extensions(permission_code)
);
INSERT INTO platform_notification_permission_extensions VALUES
  ('platform.notification_template.read','PRD-105',7),
  ('platform.notification_template.write','PRD-105',7);
INSERT INTO platform_notification_role_permissions VALUES
  ('platform_owner','platform.notification_template.read'),
  ('platform_owner','platform.notification_template.write'),
  ('platform_communications','platform.notification_template.read'),
  ('platform_communications','platform.notification_template.write');
CREATE TRIGGER trg_platform_notification_permissions_insert_prd105
BEFORE INSERT ON platform_notification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;
CREATE TRIGGER trg_platform_notification_permissions_update_prd105
BEFORE UPDATE ON platform_notification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;
CREATE TRIGGER trg_platform_notification_permissions_delete_prd105
BEFORE DELETE ON platform_notification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;
CREATE TRIGGER trg_platform_notification_role_permissions_insert_prd105
BEFORE INSERT ON platform_notification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;
CREATE TRIGGER trg_platform_notification_role_permissions_update_prd105
BEFORE UPDATE ON platform_notification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;
CREATE TRIGGER trg_platform_notification_role_permissions_delete_prd105
BEFORE DELETE ON platform_notification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_notification_permission_immutable'); END;

CREATE TABLE notification_templates(
  notification_template_id TEXT PRIMARY KEY CHECK(length(notification_template_id)=36
    AND substr(notification_template_id,1,4)='ntp_'
    AND substr(notification_template_id,5) NOT GLOB '*[^0-9a-f]*'),
  template_code TEXT NOT NULL CHECK(length(template_code) BETWEEN 3 AND 80
    AND template_code NOT GLOB '*[^a-z0-9_.-]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'security.sign_in_risk','subscription.status_changed','verification.status_changed',
    'payment.status_changed','invoice.issued','invoice.reversed',
    'infrastructure.backup_completed','gateway.health_changed')),
  category TEXT NOT NULL CHECK(category IN(
    'security','billing','verification','payments','invoices','operations')),
  channel TEXT NOT NULL CHECK(channel IN('in_app','email','webhook')),
  locale TEXT NOT NULL CHECK(locale IN('en','zh-CN')),
  version INTEGER NOT NULL CHECK(version>=1),
  parameter_allowlist_json TEXT NOT NULL CHECK(json_valid(parameter_allowlist_json)
    AND json_type(parameter_allowlist_json)='array'),
  parameter_allowlist_sha256 TEXT NOT NULL CHECK(length(parameter_allowlist_sha256)=64
    AND parameter_allowlist_sha256 NOT GLOB '*[^0-9a-f]*'),
  subject_template TEXT NOT NULL CHECK(length(subject_template) BETWEEN 1 AND 200
    AND instr(subject_template,'<')=0 AND instr(subject_template,'>')=0
    AND instr(subject_template,'{%')=0 AND instr(subject_template,'{#')=0),
  body_template TEXT NOT NULL CHECK(length(body_template) BETWEEN 1 AND 4000
    AND instr(body_template,'<')=0 AND instr(body_template,'>')=0
    AND instr(body_template,'{%')=0 AND instr(body_template,'{#')=0),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('draft','published')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  idempotency_sha256 TEXT CHECK(idempotency_sha256 IS NULL OR length(idempotency_sha256)=64),
  request_sha256 TEXT CHECK(request_sha256 IS NULL OR length(request_sha256)=64),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(template_code,channel,locale,version),
  UNIQUE(notification_template_id,event_type,channel,version),
  CHECK((status='draft' AND published_at IS NULL)
    OR(status='published' AND published_at IS NOT NULL)),
  CHECK((idempotency_sha256 IS NULL AND request_sha256 IS NULL)
    OR(idempotency_sha256 IS NOT NULL AND request_sha256 IS NOT NULL))
);
CREATE INDEX idx_notification_templates_catalog_prd105
  ON notification_templates(event_type,channel,locale,status,version DESC);
CREATE UNIQUE INDEX uq_notification_templates_idempotency_prd105
  ON notification_templates(created_by_operator_id,idempotency_sha256)
  WHERE idempotency_sha256 IS NOT NULL;
CREATE TRIGGER trg_notification_templates_update_prd105 BEFORE UPDATE ON notification_templates
BEGIN SELECT RAISE(ABORT,'notification_template_immutable'); END;
CREATE TRIGGER trg_notification_templates_delete_prd105 BEFORE DELETE ON notification_templates
BEGIN SELECT RAISE(ABORT,'notification_template_immutable'); END;

-- Seed one safe in-app template per supported event. External channel templates must be explicitly
-- published by Platform Communications before those channels can be attempted.
INSERT INTO notification_templates VALUES
('ntp_00000000000000000000000000000001','security_sign_in_risk','security.sign_in_risk',
 'security','in_app','en',1,'["event_label","occurred_at"]',
 'a357e75561beb1f5c51abb25e2a845da9453f1fbb82ec1d935ef83c430ab3ab3',
 'Security activity detected','{{event_label}} at {{occurred_at}}.',
 'aff2382935eab8928f71750dd11c549d52a75c63b258ad55335f7d205d87c660',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000002','subscription_status_changed',
 'subscription.status_changed','billing','in_app','en',1,
 '["effective_at","plan_name","status"]',
 '4092e9946562473f0b78de6fc169953f9b8a73b4df5b9cbde91dbd38b273350c',
 'Subscription updated','{{plan_name}} is {{status}} from {{effective_at}}.',
 'db0f0d2451d0fbc93657ef13e996eb4565f19b2c0e832670838c93e7f310e6fc',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000003','verification_status_changed',
 'verification.status_changed','verification','in_app','en',1,
 '["status","updated_at"]','1c208f148d02a790ad7fe712a436db7fc72eccb7eacf6db34742af29af5f076e',
 'Verification updated','Verification is {{status}} at {{updated_at}}.',
 'e442e0b26d746b9d38b25c883ea193ef60370a7e436f8f6aa0caaf0e02dfec00',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000004','payment_status_changed',
 'payment.status_changed','payments','in_app','en',1,
 '["amount","currency","status","updated_at"]',
 '79ba198b7494762af738b5d39b3228c4819ddfb84661cc793175bfba5ffa5559',
 'Payment updated','Payment {{amount}} {{currency}} is {{status}} at {{updated_at}}.',
 '75cc9f2ecd582febcc584c3dc7a2f629f098982c9872c5ca6d5acb1f9ecc4c55',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000005','invoice_issued','invoice.issued',
 'invoices','in_app','en',1,'["currency","invoice_number_masked","issued_at","total"]',
 '5566f6405daf7f3c4966f46497624d69c1e09287507a8985cdbe2bc563db8cb2',
 'Invoice issued','Invoice {{invoice_number_masked}} for {{total}} {{currency}} was issued at {{issued_at}}.',
 '2b9a6272bfd92239cd6e766036667983932eac03ab83ad484f0832635ad15146',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000006','invoice_reversed','invoice.reversed',
 'invoices','in_app','en',1,'["invoice_number_masked","reversed_at"]',
 '5960f16f744c0629ea4c9d4bed8a2133a4e26df01864e93b76c0afc2e5c042bc',
 'Invoice reversed','Invoice {{invoice_number_masked}} was reversed at {{reversed_at}}.',
 '804cc0544f6efb94b432406c8a4105f625b1656fb629d6d8d87b40ed3cc762d0',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000007','infrastructure_backup_completed',
 'infrastructure.backup_completed','operations','in_app','en',1,
 '["completed_at","status"]','dd9815636d97154839fbff636af58cd9b627dd0339594af85a1eaff2ad11d8e1',
 'Backup completed','Backup is {{status}} at {{completed_at}}.',
 'efb00f1ec058d75e8e147bed39f67dfdf794491fad3bcab582705639058db219',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL),
('ntp_00000000000000000000000000000008','gateway_health_changed',
 'gateway.health_changed','operations','in_app','en',1,
 '["cell_label","observed_at","status"]',
 '555c9bb2389a922a723c5019abd004a2df1d38202645670a9e52c55d4005a3e7',
 'Gateway health updated','{{cell_label}} is {{status}} at {{observed_at}}.',
 '6181ede7d6de161df5b18e7cd994b0f7624f38d607252ccd0e8f77758744d4fe',
 'published',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z',NULL,NULL);

CREATE TABLE notification_template_catalog_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision>=1),
  seeded_template_count INTEGER NOT NULL CHECK(seeded_template_count=8),
  seeded_catalog_sha256 TEXT NOT NULL CHECK(length(seeded_catalog_sha256)=64
    AND seeded_catalog_sha256 NOT GLOB '*[^0-9a-f]*'),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO notification_template_catalog_state VALUES(
  1,1,8,'b793a0538527422f31bfa3ed3b468fbe69cf70d3a6150fb7b84c55098b7b28c4',
  '2026-08-23T00:00:00Z');
CREATE TRIGGER trg_notification_catalog_insert_prd105
BEFORE INSERT ON notification_template_catalog_state
BEGIN SELECT RAISE(ABORT,'notification_catalog_singleton'); END;
CREATE TRIGGER trg_notification_catalog_delete_prd105
BEFORE DELETE ON notification_template_catalog_state
BEGIN SELECT RAISE(ABORT,'notification_catalog_immutable'); END;
CREATE TRIGGER trg_notification_catalog_update_prd105
BEFORE UPDATE ON notification_template_catalog_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.seeded_template_count!=OLD.seeded_template_count
  OR NEW.seeded_catalog_sha256!=OLD.seeded_catalog_sha256
  OR NEW.catalog_revision!=OLD.catalog_revision+1
BEGIN SELECT RAISE(ABORT,'notification_catalog_immutable'); END;

CREATE TABLE notifications(
  notification_id TEXT PRIMARY KEY CHECK(length(notification_id)=36
    AND substr(notification_id,1,4)='ntf_'
    AND substr(notification_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  recipient_account_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN(
    'security','billing','verification','payments','invoices','operations')),
  source_owner_type TEXT NOT NULL CHECK(length(source_owner_type) BETWEEN 3 AND 64),
  source_owner_id TEXT NOT NULL CHECK(length(source_owner_id) BETWEEN 8 AND 128),
  notification_template_id TEXT NOT NULL,
  template_version INTEGER NOT NULL CHECK(template_version>=1),
  channel TEXT NOT NULL CHECK(channel='in_app'),
  subject_text TEXT NOT NULL CHECK(length(subject_text) BETWEEN 1 AND 200
    AND instr(subject_text,'<')=0 AND instr(subject_text,'>')=0),
  body_text TEXT NOT NULL CHECK(length(body_text) BETWEEN 1 AND 4000
    AND instr(body_text,'<')=0 AND instr(body_text,'>')=0),
  safe_parameters_ciphertext TEXT NOT NULL CHECK(length(safe_parameters_ciphertext) BETWEEN 80 AND 8192),
  safe_parameters_sha256 TEXT NOT NULL CHECK(length(safe_parameters_sha256)=64
    AND safe_parameters_sha256 NOT GLOB '*[^0-9a-f]*'),
  read_at TEXT CHECK(read_at IS NULL OR unixepoch(read_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(recipient_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(notification_template_id,event_type,channel,template_version)
    REFERENCES notification_templates(notification_template_id,event_type,channel,version),
  UNIQUE(workspace_id,recipient_account_id,event_type,source_owner_type,source_owner_id)
);
CREATE INDEX idx_notifications_inbox_prd105
  ON notifications(workspace_id,recipient_account_id,created_at DESC,notification_id DESC);
CREATE INDEX idx_notifications_unread_prd105
  ON notifications(workspace_id,recipient_account_id,read_at,created_at DESC);
CREATE TRIGGER trg_notifications_update_prd105 BEFORE UPDATE ON notifications FOR EACH ROW
WHEN NEW.notification_id!=OLD.notification_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.recipient_account_id!=OLD.recipient_account_id
  OR NEW.event_type!=OLD.event_type OR NEW.category!=OLD.category
  OR NEW.source_owner_type!=OLD.source_owner_type OR NEW.source_owner_id!=OLD.source_owner_id
  OR NEW.notification_template_id!=OLD.notification_template_id
  OR NEW.template_version!=OLD.template_version OR NEW.channel!=OLD.channel
  OR NEW.subject_text!=OLD.subject_text OR NEW.body_text!=OLD.body_text
  OR NEW.safe_parameters_ciphertext!=OLD.safe_parameters_ciphertext
  OR NEW.safe_parameters_sha256!=OLD.safe_parameters_sha256 OR NEW.created_at!=OLD.created_at
  OR OLD.read_at IS NOT NULL OR NEW.read_at IS NULL
BEGIN SELECT RAISE(ABORT,'notification_immutable'); END;
CREATE TRIGGER trg_notifications_delete_prd105 BEFORE DELETE ON notifications
BEGIN SELECT RAISE(ABORT,'notification_immutable'); END;

CREATE TABLE notification_preferences(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  recipient_account_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN(
    'security','billing','verification','payments','invoices','operations')),
  channel TEXT NOT NULL CHECK(channel IN('in_app','email','webhook','sms','web_push')),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  mandatory_security INTEGER NOT NULL CHECK(mandatory_security IN(0,1)),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,recipient_account_id,category,channel),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(recipient_account_id) REFERENCES global_accounts(account_id),
  CHECK((category='security' AND mandatory_security=1
      AND (enabled=1 OR channel IN('sms','web_push')))
    OR(category!='security' AND mandatory_security=0)),
  CHECK(channel NOT IN('sms','web_push') OR enabled=0)
);
CREATE TRIGGER trg_notification_preferences_update_prd105
BEFORE UPDATE ON notification_preferences FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.recipient_account_id!=OLD.recipient_account_id OR NEW.category!=OLD.category
  OR NEW.channel!=OLD.channel OR NEW.mandatory_security!=OLD.mandatory_security
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR (OLD.mandatory_security=1 AND NEW.enabled!=1
    AND NEW.channel NOT IN('sms','web_push'))
  OR (NEW.channel IN('sms','web_push') AND NEW.enabled!=0)
BEGIN SELECT RAISE(ABORT,'notification_preference_invalid'); END;
CREATE TRIGGER trg_notification_preferences_delete_prd105
BEFORE DELETE ON notification_preferences
BEGIN SELECT RAISE(ABORT,'notification_preference_immutable'); END;

CREATE TABLE channel_suppressions(
  channel_suppression_id TEXT PRIMARY KEY CHECK(length(channel_suppression_id)=36
    AND substr(channel_suppression_id,1,4)='nsp_'),
  recipient_blind_index TEXT NOT NULL CHECK(length(recipient_blind_index)=64
    AND recipient_blind_index NOT GLOB '*[^0-9a-f]*'),
  channel TEXT NOT NULL CHECK(channel IN('email','webhook')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'hard_bounce','complaint','endpoint_gone','operator_block')),
  source_code TEXT NOT NULL CHECK(length(source_code) BETWEEN 3 AND 64),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(recipient_blind_index,channel,reason_code)
);
CREATE INDEX idx_channel_suppressions_lookup_prd105
  ON channel_suppressions(recipient_blind_index,channel,expires_at);
CREATE TRIGGER trg_channel_suppressions_update_prd105 BEFORE UPDATE ON channel_suppressions
BEGIN SELECT RAISE(ABORT,'channel_suppression_immutable'); END;
CREATE TRIGGER trg_channel_suppressions_delete_prd105 BEFORE DELETE ON channel_suppressions
BEGIN SELECT RAISE(ABORT,'channel_suppression_immutable'); END;

CREATE TABLE notification_outbox(
  notification_outbox_id TEXT PRIMARY KEY CHECK(length(notification_outbox_id)=36
    AND substr(notification_outbox_id,1,4)='nob_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  recipient_account_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN(
    'security','billing','verification','payments','invoices','operations')),
  source_owner_type TEXT NOT NULL CHECK(length(source_owner_type) BETWEEN 3 AND 64),
  source_owner_id TEXT NOT NULL CHECK(length(source_owner_id) BETWEEN 8 AND 128),
  safe_parameters_ciphertext TEXT NOT NULL CHECK(length(safe_parameters_ciphertext) BETWEEN 80 AND 8192),
  safe_parameters_sha256 TEXT NOT NULL CHECK(length(safe_parameters_sha256)=64
    AND safe_parameters_sha256 NOT GLOB '*[^0-9a-f]*'),
  template_snapshot_json TEXT NOT NULL CHECK(json_valid(template_snapshot_json)
    AND json_type(template_snapshot_json)='object'),
  status TEXT NOT NULL CHECK(status IN('pending','processing','succeeded','failed','suppressed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 8),
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT CHECK(lease_token IS NULL OR length(lease_token)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR safe_error_code IN(
    'notification.template_invalid','notification.recipient_unavailable',
    'notification.delivery_unavailable','notification.suppressed')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(recipient_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,recipient_account_id,event_type,source_owner_type,source_owner_id),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL)
    OR(status='processing' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL)
    OR(status IN('succeeded','failed','suppressed') AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL AND next_attempt_at IS NULL))
);
CREATE INDEX idx_notification_outbox_due_prd105
  ON notification_outbox(status,next_attempt_at,lease_expires_at,created_at);
CREATE INDEX idx_notification_outbox_fair_prd105
  ON notification_outbox(workspace_id,status,next_attempt_at,created_at);
CREATE TRIGGER trg_notification_outbox_delete_prd105 BEFORE DELETE ON notification_outbox
BEGIN SELECT RAISE(ABORT,'notification_outbox_immutable'); END;
CREATE TRIGGER trg_notification_outbox_owner_prd105
BEFORE UPDATE ON notification_outbox FOR EACH ROW
WHEN NEW.notification_outbox_id!=OLD.notification_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.recipient_account_id!=OLD.recipient_account_id OR NEW.event_type!=OLD.event_type
  OR NEW.category!=OLD.category OR NEW.source_owner_type!=OLD.source_owner_type
  OR NEW.source_owner_id!=OLD.source_owner_id
  OR NEW.safe_parameters_ciphertext!=OLD.safe_parameters_ciphertext
  OR NEW.safe_parameters_sha256!=OLD.safe_parameters_sha256
  OR NEW.template_snapshot_json!=OLD.template_snapshot_json OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.lease_generation<=OLD.lease_generation
BEGIN SELECT RAISE(ABORT,'notification_outbox_owner_immutable'); END;

CREATE TABLE delivery_attempts(
  delivery_attempt_id TEXT PRIMARY KEY CHECK(length(delivery_attempt_id)=36
    AND substr(delivery_attempt_id,1,4)='nda_'),
  notification_outbox_id TEXT NOT NULL,
  notification_id TEXT,
  channel TEXT NOT NULL CHECK(channel IN('in_app','email','webhook','sms','web_push')),
  provider_code TEXT NOT NULL CHECK(provider_code IN(
    'internal','email_adapter','webhook_adapter','unconfigured')),
  attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 8),
  lease_generation INTEGER NOT NULL CHECK(lease_generation>=1),
  preference_enabled INTEGER NOT NULL CHECK(preference_enabled IN(0,1)),
  mandatory_security INTEGER NOT NULL CHECK(mandatory_security IN(0,1)),
  status TEXT NOT NULL CHECK(status IN('sent','suppressed','unavailable','failed')),
  safe_error_code TEXT CHECK(safe_error_code IS NULL OR safe_error_code IN(
    'notification.channel_disabled','notification.channel_unavailable',
    'notification.recipient_unavailable','notification.suppressed',
    'notification.delivery_unavailable')),
  attempted_at TEXT NOT NULL CHECK(unixepoch(attempted_at) IS NOT NULL),
  FOREIGN KEY(notification_outbox_id) REFERENCES notification_outbox(notification_outbox_id),
  FOREIGN KEY(notification_id) REFERENCES notifications(notification_id),
  UNIQUE(notification_outbox_id,channel,attempt_number),
  CHECK((status='sent' AND safe_error_code IS NULL)
    OR(status!='sent' AND safe_error_code IS NOT NULL)),
  CHECK(mandatory_security=0 OR preference_enabled=1 OR channel IN('sms','web_push'))
);
CREATE INDEX idx_delivery_attempts_outbox_prd105
  ON delivery_attempts(notification_outbox_id,attempt_number,channel);
CREATE TRIGGER trg_delivery_attempts_update_prd105 BEFORE UPDATE ON delivery_attempts
BEGIN SELECT RAISE(ABORT,'notification_delivery_attempt_immutable'); END;
CREATE TRIGGER trg_delivery_attempts_delete_prd105 BEFORE DELETE ON delivery_attempts
BEGIN SELECT RAISE(ABORT,'notification_delivery_attempt_immutable'); END;

CREATE TEMP TABLE prd105_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd105_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_roles WHERE role_code='platform_communications')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_notification_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_notification_role_permissions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM notification_templates)=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'notification_templates','notification_template_catalog_state','notifications',
      'notification_preferences','delivery_attempts','channel_suppressions',
      'notification_outbox'))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd105_up_final_guard;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
