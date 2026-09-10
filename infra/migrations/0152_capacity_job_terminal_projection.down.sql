BEGIN IMMEDIATE;
CREATE TEMP TABLE capacity_projection_down_guard(n INTEGER CHECK(n=0));
INSERT INTO capacity_projection_down_guard
  SELECT COUNT(*) FROM platform_jobs WHERE type_code='capacity.run';
DROP TABLE capacity_projection_down_guard;
DROP TRIGGER trg_capacity_job_terminal_projection;
COMMIT;
