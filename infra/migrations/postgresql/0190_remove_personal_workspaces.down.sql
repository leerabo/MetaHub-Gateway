-- Personal Workspaces were intentionally removed and cannot be restored by rollback.
DO $$
BEGIN
  RAISE EXCEPTION 'personal workspaces were removed and cannot be restored';
END
$$;
