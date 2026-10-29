DO $$ BEGIN
IF EXISTS(SELECT 1 FROM local_registration_consents) THEN
RAISE EXCEPTION 'local_registration_consents_not_empty'; END IF;
END $$;
DROP TABLE local_registration_consents;
DROP FUNCTION reject_local_registration_consent_change();
DROP TABLE registration_captchas;
DROP TABLE registration_policy;
