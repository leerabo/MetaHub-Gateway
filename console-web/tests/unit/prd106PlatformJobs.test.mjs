import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  cancelPlatformJob,
  commandPlatformJobType,
  getPlatformJob,
  listPlatformJobQueues,
} from "../../src/lib/platformJobs.js";

const CONTROL_URL = "http://control.test";
const JOB_ID = `pjob_${"1".repeat(32)}`;
const ATTEMPT_ID = `pja_${"2".repeat(32)}`;
const CHECKPOINT_ID = `pjc_${"3".repeat(32)}`;
const NOW = "2026-08-23T08:00:00Z";

test("platform job client rejects response extensions and keeps safe DTOs", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse(queueResponse());
    assert.equal(
      (await listPlatformJobQueues({ controlApiUrl: CONTROL_URL })).recent_jobs[0].job_id,
      JOB_ID,
    );
    globalThis.fetch = async () => jsonResponse({ ...detailResponse(), input_envelope: "hidden" });
    await assert.rejects(
      getPlatformJob({ controlApiUrl: CONTROL_URL, jobId: JOB_ID }),
      (error) => error.code === "platform_job.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("job and type commands bind Cookie CSRF CAS and idempotency without bearer", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return String(url).endsWith("/platform/job-types")
      ? jsonResponse(typeResponse())
      : jsonResponse(actionResponse());
  };
  try {
    await cancelPlatformJob({
      controlApiUrl: CONTROL_URL,
      jobId: JOB_ID,
      revision: 4,
      reasonCode: "operator_cancelled",
      idempotencyKey: "prd106-console-cancel-0001",
    });
    await commandPlatformJobType({
      controlApiUrl: CONTROL_URL,
      typeCode: "platform.runtime.probe",
      action: "disable",
      reasonCode: "operational_pause",
      revision: 1,
      idempotencyKey: "prd106-console-type-000001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls.length, 2);
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls.every((call) => !("Authorization" in call.options.headers)), true);
  assert.equal(calls[0].options.headers["If-Match"], '"4"');
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd106-console-cancel-0001");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    reason_code: "operator_cancelled",
    confirmed: true,
  });
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
});

test("job runtime renderer prunes write controls and never renders encrypted facts", () => {
  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.job.read", "platform.job.manage", "platform.job.type_manage",
  ]));
  assert.match(writable.innerHTML, /任务运行时/u);
  assert.match(writable.innerHTML, /data-platform-job-command-form/u);
  assert.match(writable.innerHTML, /data-platform-job-type-form/u);
  assert.match(writable.innerHTML, /Attempt 时间线/u);
  assert.doesNotMatch(writable.innerHTML, /input_envelope|checkpoint_envelope|lease_token/u);

  const readonly = { innerHTML: "" };
  renderPlatformWorkspace(readonly, platformState(["platform.job.read"]));
  assert.match(readonly.innerHTML, /任务类型/u);
  assert.doesNotMatch(
    readonly.innerHTML,
    /data-platform-job-command-form|data-platform-job-type-form/u,
  );
});

test("job tables collapse to labeled rows on the 390px viewport", async () => {
  const css = await readFile(
    new URL("../../src/styles/platform.css", import.meta.url),
    "utf8",
  );
  assert.match(css, /\.platform-job-table, \.platform-job-type-table \{ min-width: 0;/u);
  assert.match(css, /\.platform-job-table thead, \.platform-job-type-table thead \{ display: none;/u);
  assert.match(css, /content: attr\(data-label\)/u);
});

test("external observation DTO accepts only safe, coherent lifecycle fields", async () => {
  const originalFetch = globalThis.fetch;
  const payload = { ...detailResponse(), external_wait: externalWait() };
  payload.job = { ...payload.job, status: "running", completed_at: null };
  try {
    globalThis.fetch = async () => jsonResponse(payload);
    assert.equal((await getPlatformJob({ controlApiUrl: CONTROL_URL, jobId: JOB_ID }))
      .external_wait.observation_count, 25);
    for (const mutation of [
      { lease_token: "forbidden" }, { continuation_envelope: "forbidden" },
      { observation_count: -1 }, { acknowledged: "yes" }, { status: "completed" },
    ]) {
      globalThis.fetch = async () => jsonResponse({
        ...payload, external_wait: { ...payload.external_wait, ...mutation },
      });
      await assert.rejects(getPlatformJob({ controlApiUrl: CONTROL_URL, jobId: JOB_ID }),
        (error) => error.code === "platform_job.response_invalid");
    }
    globalThis.fetch = async () => jsonResponse({
      ...queueResponse(), queues: queueResponse().queues.map((q) => ({ ...q, external_wait_count: 3 })),
    });
    assert.equal((await listPlatformJobQueues({ controlApiUrl: CONTROL_URL })).queues[0].external_wait_count, 3);
  } finally { globalThis.fetch = originalFetch; }
});

test("external wait renders stopping and prevents terminal execution replay", () => {
  const state = platformState(["platform.job.read", "platform.job.manage"]);
  state.selectedJob.external_wait = externalWait();
  state.selectedJob.job.status = "cancel_requested";
  const root = { innerHTML: "" };
  renderPlatformWorkspace(root, state);
  assert.match(root.innerHTML, /等待停止确认/u);
  assert.doesNotMatch(root.innerHTML, /data-platform-job-command-form/u);
  state.selectedJob.job.status = "failed";
  state.selectedJob.external_wait = { ...externalWait(), status: "completed", completed_at: NOW };
  renderPlatformWorkspace(root, state);
  assert.match(root.innerHTML, /执行已结束/u);
  assert.doesNotMatch(root.innerHTML, /data-platform-job-command-form/u);
});

function externalWait() {
  return {
    status: "waiting", resume_generation: 25, acknowledged: true, observation_count: 25,
    failure_count: 0, safe_outcome_code: null, deadline_at: NOW, next_poll_at: NOW,
    updated_at: NOW, completed_at: null,
  };
}

function platformState(permissions) {
  return {
    session: {
      context: { permissions, operator: { display_name: "Ops", roles: ["platform_ops"] } },
    },
    navigation: [
      { id: "job-runtime", label: "Job runtime", href: "/platform.html#job-runtime" },
    ],
    view: "job-runtime",
    jobQueues: queueResponse(),
    selectedJob: detailResponse(),
    jobStatus: "",
    loading: false,
  };
}

function queueResponse() {
  return {
    queues: [queue("critical", 100), queue("standard", 50), queue("bulk", 10)],
    recent_jobs: [job()],
    job_types: [jobType()],
    request_id: "request-jobs",
    trace_id: "trace-jobs",
  };
}

function queue(queueCode, priorityWeight) {
  return {
    queue_code: queueCode,
    priority_weight: priorityWeight,
    max_concurrency: 8,
    queued_count: 1,
    active_lease_count: 0,
    dead_letter_count: 0,
    oldest_wait_seconds: 3,
  };
}

function job() {
  return {
    job_id: JOB_ID,
    type_code: "platform.runtime.probe",
    queue_code: "standard",
    priority: 50,
    status: "failed",
    revision: 4,
    generation: 1,
    attempt_count: 1,
    max_attempts: 3,
    safe_outcome_code: "platform_job.probe_failed",
    scheduled_at: NOW,
    started_at: NOW,
    completed_at: NOW,
    created_at: NOW,
    updated_at: NOW,
  };
}

function jobType() {
  return {
    type_code: "platform.runtime.probe",
    contract_version: 1,
    queue_code: "standard",
    resource_class: "control",
    max_attempts: 3,
    timeout_seconds: 30,
    status: "active",
    revision: 1,
  };
}

function detailResponse() {
  return {
    job: job(),
    attempts: [{
      attempt_id: ATTEMPT_ID,
      attempt_number: 1,
      generation: 1,
      status: "failed",
      safe_outcome_code: "platform_job.probe_failed",
      leased_at: NOW,
      started_at: NOW,
      completed_at: NOW,
    }],
    checkpoints: [{
      checkpoint_id: CHECKPOINT_ID,
      generation: 1,
      sequence_number: 1,
      checkpoint_sha256: "a".repeat(64),
      created_at: NOW,
    }],
    request_id: "request-job-detail",
    trace_id: "trace-job-detail",
  };
}

function actionResponse() {
  return {
    job: { ...job(), status: "cancelled" },
    idempotent_replay: false,
    request_id: "request-job-action",
    trace_id: "trace-job-action",
  };
}

function typeResponse() {
  return {
    job_type: { ...jobType(), status: "disabled", revision: 2 },
    idempotent_replay: false,
    request_id: "request-type-action",
    trace_id: "trace-type-action",
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
