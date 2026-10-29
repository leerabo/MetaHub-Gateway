-- metahub:dev-registration-defaults-v1
-- Local development is intentionally frictionless. Production and test
-- policies remain unchanged; platform operators can still opt into CAPTCHA.
UPDATE registration_policy
SET human_verification=0,
    revision=revision+1
WHERE environment='dev' AND human_verification<>0;
