CREATE TABLE registration_policy(
  environment TEXT PRIMARY KEY CHECK(environment IN('dev','test','prod')),
  email_verification INTEGER NOT NULL CHECK(email_verification IN(0,1)),
  human_verification INTEGER NOT NULL CHECK(human_verification IN(0,1)),
  revision INTEGER NOT NULL CHECK(revision>0)
);
INSERT INTO registration_policy VALUES('dev',0,1,1),('test',1,0,1),('prod',1,1,1);
CREATE TABLE registration_captchas(
  challenge_id TEXT PRIMARY KEY,
  answer_hmac TEXT NOT NULL,
  device_hmac TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
  consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN(0,1))
);
CREATE INDEX idx_registration_captcha_expiry ON registration_captchas(expires_at);
CREATE TABLE local_registration_consents(
  registration_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES global_accounts(account_id),
  consent_version TEXT NOT NULL,
  consented_at TEXT NOT NULL,
  email_verified INTEGER NOT NULL DEFAULT 0 CHECK(email_verified=0)
);
CREATE FUNCTION reject_local_registration_consent_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'local_registration_consent_immutable'; END $$;
CREATE TRIGGER trg_local_registration_consent_change BEFORE UPDATE OR DELETE
ON local_registration_consents FOR EACH ROW EXECUTE FUNCTION reject_local_registration_consent_change();
