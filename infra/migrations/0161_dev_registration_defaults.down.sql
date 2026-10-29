UPDATE registration_policy
SET human_verification=1,
    revision=revision+1
WHERE environment='dev' AND human_verification=0;
