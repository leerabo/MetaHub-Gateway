BEGIN IMMEDIATE;
CREATE TEMP TABLE ga_down_guard(value INTEGER);
CREATE TEMP TRIGGER ga_down_facts_guard BEFORE INSERT ON ga_down_guard
WHEN EXISTS(SELECT 1 FROM ga_launches) OR EXISTS(SELECT 1 FROM ga_rollbacks)
  OR EXISTS(SELECT 1 FROM post_ga_reviews) OR EXISTS(SELECT 1 FROM ga_evidence)
  OR EXISTS(SELECT 1 FROM ga_decisions) OR EXISTS(SELECT 1 FROM ga_action_commands)
  OR EXISTS(SELECT 1 FROM ga_events) OR EXISTS(SELECT 1 FROM ga_event_outbox)
BEGIN SELECT RAISE(ROLLBACK,'ga_facts_present'); END;
INSERT INTO ga_down_guard VALUES(1);
DROP TABLE ga_down_guard;
DROP TABLE ga_action_commands;
DROP TABLE ga_event_outbox;
DROP TABLE ga_events;
DROP TABLE post_ga_reviews;
DROP TABLE ga_rollbacks;
DROP TABLE ga_evidence;
DROP TABLE ga_decisions;
DROP TABLE ga_launches;
DROP TABLE ga_gate_policies;
COMMIT;
