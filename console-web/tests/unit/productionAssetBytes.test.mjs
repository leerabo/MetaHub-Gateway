import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { productionAssetBytes } from "../../scripts/production-asset-bytes.mjs";

test("production text bytes and hashes are identical for LF and CRLF checkouts", () => {
  for (const extension of ["html", "css", "js"]) {
    const lf = Buffer.from("line one\nline two\n");
    const crlf = Buffer.from("line one\r\nline two\r\n");
    const left = productionAssetBytes(`asset.${extension}`, lf);
    const right = productionAssetBytes(`asset.${extension}`, crlf);
    assert.deepEqual(left, lf);
    assert.deepEqual(right, lf);
    assert.equal(left.byteLength, right.byteLength);
    assert.equal(createHash("sha256").update(left).digest("hex"),
      createHash("sha256").update(right).digest("hex"));
  }
});

test("non-text production assets retain their exact bytes", () => {
  const binary = Buffer.from([0, 255, 13, 10, 128]);
  assert.deepEqual(productionAssetBytes("image.png", binary), binary);
});
