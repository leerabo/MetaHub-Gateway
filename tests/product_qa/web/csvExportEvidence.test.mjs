import assert from "node:assert/strict";
import test from "node:test";

import { requestUsageCsvExport, safeUsageCsvFilename } from "../../../console-web/src/lib/usage.js";

function csvResponse(text, headers = {}) {
  return new Response(`\ufeff${text}`, {
    status: 200,
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": 'attachment; filename="usage-qa.csv"',
      "X-Request-ID": "rq_csv_qa",
      ...headers,
    },
  });
}

test("UI/API CSV export evidence preserves UTF-8 BOM, CRLF, and safe filename", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => csvResponse("column_a,column_b\r\nplain,0\r\n");
    const result = await requestUsageCsvExport({ controlApiUrl: "http://127.0.0.1:8001", token: "qa-runtime-token", exportRequestId: "rq_csv_qa", exportTraceId: "tr_csv_qa" });
    const bytes = new Uint8Array(await result.blob.arrayBuffer());
    assert.deepEqual([...bytes.slice(0, 3)], [0xef, 0xbb, 0xbf]);
    assert.equal(new TextDecoder().decode(bytes).endsWith("\r\n"), true);
    assert.equal(result.filename, "usage-qa.csv");
    assert.equal(safeUsageCsvFilename('attachment; filename="../private.csv"'), "metahub-usage-export.csv");
    assert.doesNotMatch(new TextDecoder().decode(bytes), /qa-runtime-token|prompt|response/i);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CSV export evidence rejects formula injection before it can be opened by a spreadsheet", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => csvResponse("column_a,column_b\r\n'=2+2,'+SUM(A1)\r\n");
    const result = await requestUsageCsvExport({ controlApiUrl: "http://127.0.0.1:8001", token: "qa-runtime-token" });
    const text = new TextDecoder().decode(new Uint8Array(await result.blob.arrayBuffer()));
    const cells = text.replace(/^\ufeff/u, "").split("\r\n")[1].split(",");
    assert.ok(cells.every((cell) => /^[']/.test(cell)));
    assert.ok(cells.every((cell) => !/^[=+\-@]/.test(cell)));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CSV export evidence rejects non-CSV HTTP 200 payloads", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("<html>not csv</html>", { status: 200, headers: { "Content-Type": "text/html" } });
    await assert.rejects(
      requestUsageCsvExport({ controlApiUrl: "http://127.0.0.1:8001", token: "qa-runtime-token" }),
      (error) => error.code === "usage.export_unavailable" && error.status === 200,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
