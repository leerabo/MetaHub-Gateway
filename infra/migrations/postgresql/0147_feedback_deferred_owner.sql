-- Restore the original atomic Feedback/Event cycle without changing any stored facts.
LOCK TABLE request_feedbacks,request_feedback_events IN ACCESS EXCLUSIVE MODE;
DO $guard$
BEGIN
  IF (SELECT count(*) FROM pg_constraint
      WHERE (conrelid,conname,confrelid,conkey,confkey) IN (
        ('request_feedbacks'::regclass,'fk_request_feedbacks_0_request_feedback_events',
         'request_feedback_events'::regclass,
         ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='request_feedbacks'::regclass AND attname='tenant_id'),
               (SELECT attnum FROM pg_attribute WHERE attrelid='request_feedbacks'::regclass AND attname='feedback_event_id')]::smallint[],
         ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='request_feedback_events'::regclass AND attname='tenant_id'),
               (SELECT attnum FROM pg_attribute WHERE attrelid='request_feedback_events'::regclass AND attname='feedback_event_id')]::smallint[]),
        ('request_feedback_events'::regclass,'fk_request_feedback_events_0_request_feedbacks',
         'request_feedbacks'::regclass,
         ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='request_feedback_events'::regclass AND attname='tenant_id'),
               (SELECT attnum FROM pg_attribute WHERE attrelid='request_feedback_events'::regclass AND attname='request_feedback_id')]::smallint[],
         ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='request_feedbacks'::regclass AND attname='tenant_id'),
               (SELECT attnum FROM pg_attribute WHERE attrelid='request_feedbacks'::regclass AND attname='request_feedback_id')]::smallint[])
      ) AND contype='f' AND convalidated AND NOT condeferrable AND NOT condeferred
        AND confupdtype='a' AND confdeltype='a' AND confmatchtype='s') <> 2
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='feedback_owner_constraint_drift';
  END IF;
END;
$guard$;
ALTER TABLE request_feedbacks ALTER CONSTRAINT fk_request_feedbacks_0_request_feedback_events
  DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE request_feedback_events ALTER CONSTRAINT fk_request_feedback_events_0_request_feedbacks
  DEFERRABLE INITIALLY DEFERRED;
