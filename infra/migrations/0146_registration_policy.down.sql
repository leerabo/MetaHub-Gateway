BEGIN IMMEDIATE;
CREATE TEMP TABLE registration_policy_down_guard(n INTEGER CHECK(n=0));
INSERT INTO registration_policy_down_guard SELECT count(*) FROM local_registration_consents;
DROP TABLE registration_policy_down_guard;
DROP TABLE local_registration_consents;
DROP TABLE registration_captchas;
DROP TABLE registration_policy;
COMMIT;
