-- Retaining repaired guards while marking this migration rolled back would make
-- its guarded forward migration impossible to reapply. Refuse the downgrade
-- atomically, preserving both the guards and the applied migration record.
DO $$
BEGIN
  RAISE EXCEPTION '0194 is forward-only; recover a verified matching backup instead'
    USING ERRCODE = '0A000';
END;
$$;
