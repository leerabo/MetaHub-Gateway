-- Platform-wide SMTP credentials are encrypted by the application, never stored in plaintext.
CREATE TABLE platform_smtp_settings (
  environment TEXT PRIMARY KEY CHECK(environment IN ('dev','test','prod')),
  settings_ciphertext TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL REFERENCES platform_operators(operator_id),
  connection_tested_at TEXT,
  email_tested_at TEXT
);
