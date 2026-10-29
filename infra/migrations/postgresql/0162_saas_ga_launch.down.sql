DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM ga_launches) OR EXISTS(SELECT 1 FROM ga_rollbacks)
    OR EXISTS(SELECT 1 FROM post_ga_reviews) OR EXISTS(SELECT 1 FROM ga_evidence)
    OR EXISTS(SELECT 1 FROM ga_decisions) OR EXISTS(SELECT 1 FROM ga_action_commands)
    OR EXISTS(SELECT 1 FROM ga_events) OR EXISTS(SELECT 1 FROM ga_event_outbox)
    THEN RAISE EXCEPTION 'ga_facts_present'; END IF;
END $$;
DROP TABLE ga_action_commands;
DROP TABLE ga_event_outbox;
DROP TABLE ga_events;
DROP TABLE post_ga_reviews;
DROP TABLE ga_rollbacks;
DROP TABLE ga_evidence;
DROP TABLE ga_decisions;
DROP TABLE ga_launches;
DROP TABLE ga_gate_policies;
DROP FUNCTION ga_gate_immutable();
DROP FUNCTION ga_launch_guard();
DROP FUNCTION ga_immutable();
DROP FUNCTION ga_rollback_guard();
