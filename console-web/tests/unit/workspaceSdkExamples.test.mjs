import assert from "node:assert/strict";
import test from "node:test";
import { AuthApiError } from "../../src/lib/auth.js";
import { getPythonSdkExample, getJavascriptSdkExample, downloadPythonSdkExample,
  downloadJavascriptSdkExample } from "../../src/lib/pythonSdkExamples.js";

const clients = [
  ["python", getPythonSdkExample, false], ["javascript", getJavascriptSdkExample, false],
  ["python", downloadPythonSdkExample, true], ["javascript", downloadJavascriptSdkExample, true],
];
for (const [language, client, download] of clients) {
  test(`Workspace SDK ${language} ${download ? "download" : "metadata"} uses Cookie and cancellation`, async (t) => {
    const controller = new AbortController();
    const archive = new Blob(["PK-test"], { type: "application/zip" });
    const calls = [];
    t.mock.method(globalThis, "fetch", async (url, options) => {
      calls.push({ url, options });
      return new Response(download ? archive : JSON.stringify({ language }), { headers: {
        "Content-Disposition": 'attachment; filename="../../private.zip"', "X-Request-ID": "req_sdk_test",
      } });
    });
    const result = await client({ controlApiUrl: "https://control.example.test", authentication: "workspace",
      signal: controller.signal, language: "../../ignored", tenant_id: "ignored" });
    assert.equal(calls[0].url, `https://control.example.test/developer/sdk-examples/${language}${download ? "/download" : ""}`);
    assert.equal(calls[0].options.credentials, "include");
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.equal(calls[0].options.cache, "no-store");
    assert.equal(calls[0].options.redirect, "error");
    assert.equal(calls[0].options.signal, controller.signal);
    if (download) {
      assert.equal(result.filename, `metahub-${language}-examples.zip`);
      assert.equal(await result.blob.text(), "PK-test");
      assert.equal(result.requestId, "req_sdk_test");
    } else assert.equal(result.language, language);

    await assert.rejects(client({ controlApiUrl: "https://control.example.test", authentication: "workspace",
      token: "synthetic-conflicting-token" }), (error) => error instanceof AuthApiError
        && error.code === "workspace_session.credential_conflict");
    assert.equal(calls.length, 1);
  });
}

test("Workspace SDK passes an aborted signal and preserves abort rejection", async (t) => {
  const controller = new AbortController();
  controller.abort();
  t.mock.method(globalThis, "fetch", async (_url, { signal }) => signal.throwIfAborted());
  await assert.rejects(getPythonSdkExample({ controlApiUrl: "https://control.example.test",
    authentication: "workspace", signal: controller.signal }), { name: "AbortError" });
});
