-- Keep all facts and restore the prior immediate checks only when the owner graph is intact.
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
      ) AND contype='f' AND convalidated AND condeferrable AND condeferred
        AND confupdtype='a' AND confdeltype='a' AND confmatchtype='s') <> 2
    OR EXISTS(SELECT 1 FROM request_feedbacks feedback LEFT JOIN request_feedback_events event
      ON event.tenant_id=feedback.tenant_id AND event.feedback_event_id=feedback.feedback_event_id
      WHERE event.feedback_event_id IS NULL)
    OR EXISTS(SELECT 1 FROM request_feedback_events event LEFT JOIN request_feedbacks feedback
      ON feedback.tenant_id=event.tenant_id AND feedback.request_feedback_id=event.request_feedback_id
      WHERE feedback.request_feedback_id IS NULL)
  THEN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='feedback_owner_constraint_drift';
  END IF;
END;
$guard$;
SET CONSTRAINTS fk_request_feedbacks_0_request_feedback_events,
  fk_request_feedback_events_0_request_feedbacks IMMEDIATE;
ALTER TABLE request_feedbacks ALTER CONSTRAINT fk_request_feedbacks_0_request_feedback_events
  NOT DEFERRABLE INITIALLY IMMEDIATE;
ALTER TABLE request_feedback_events ALTER CONSTRAINT fk_request_feedback_events_0_request_feedbacks
  NOT DEFERRABLE INITIALLY IMMEDIATE;
