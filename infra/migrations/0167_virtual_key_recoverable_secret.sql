-- metahub: virtual-key-recoverable-secret-v1
-- Store only an application-encrypted copy so an authorized user can copy a key later.
ALTER TABLE virtual_keys ADD COLUMN encrypted_key TEXT;
