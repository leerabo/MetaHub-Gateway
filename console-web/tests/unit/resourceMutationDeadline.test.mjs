import assert from "node:assert/strict";
import test from "node:test";
import { withResourceMutationDeadline } from "../../src/lib/resourceMutationDeadline.js";

test("write timeout reports an unknown outcome, aborts transport and never retries", async () => {
  let calls = 0;
  let transportSignal;
  const result = withResourceMutationDeadline((signal) => {
    calls += 1;
    transportSignal = signal;
    return new Promise(() => {});
  }, undefined, { timeoutMs: 5, requestId: "req_deadline" });
  await assert.rejects(result, { code: "resource.mutation_unconfirmed", status: 0, requestId: "req_deadline" });
  assert.equal(transportSignal.aborted, true);
  assert.equal(calls, 1);
});

test("write deadline covers delayed response bodies and ignores late success", async () => {
  let resolve;
  const result = withResourceMutationDeadline(() => new Promise((accept) => { resolve = accept; }), undefined, { timeoutMs: 5 });
  await assert.rejects(result, { code: "resource.mutation_unconfirmed" });
  resolve({ state: "ok" });
  await assert.rejects(result, { code: "resource.mutation_unconfirmed" });
});

test("owner cancellation is not reported as an unknown write outcome", async () => {
  const owner = new AbortController();
  let transportSignal;
  const result = withResourceMutationDeadline((signal) => {
    transportSignal = signal;
    return new Promise(() => {});
  }, owner.signal, { timeoutMs: 500 });
  owner.abort();
  await assert.rejects(result, { name: "AbortError" });
  assert.equal(transportSignal.aborted, true);
});

test("cancelled owners never dispatch and settled requests retain their actual result", async () => {
  const owner = new AbortController();
  owner.abort();
  let calls = 0;
  await assert.rejects(withResourceMutationDeadline(() => { calls += 1; }, owner.signal), { name: "AbortError" });
  assert.equal(calls, 0);
  assert.equal(await withResourceMutationDeadline(async () => "saved", undefined, { timeoutMs: 500 }), "saved");
  const error = new Error("rejected");
  await assert.rejects(withResourceMutationDeadline(() => { throw error; }, undefined, { timeoutMs: 500 }), (value) => value === error);
});
