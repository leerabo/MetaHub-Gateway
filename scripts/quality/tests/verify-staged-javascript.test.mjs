import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const script = fileURLToPath(new URL("../verify-staged-javascript.mjs", import.meta.url));

async function fixture(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "metahub-index-js-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const git = (args) => execFileSync("git", args, { cwd: directory, stdio: "pipe" });
  git(["init", "--quiet"]);
  return {
    directory, git,
    write: (name, source) => writeFile(path.join(directory, name), source),
    run: () => {
      const result = spawnSync(process.execPath, [script], { cwd: directory, encoding: "utf8" });
      return { status: result.status, report: JSON.parse(result.stdout) };
    },
  };
}

test("valid worktree cannot hide a broken staged module", async (t) => {
  const f = await fixture(t);
  await f.write("entry.js", "export const = 1;");
  f.git(["add", "entry.js"]);
  await f.write("entry.js", "export const value = 1;");
  assert.deepEqual(f.run(), { status: 1, report: {
    scope: "git index JavaScript syntax only", checked: 1, failures: ["entry.js"],
  } });
});

test("unstaged syntax is excluded and an empty index is accepted", async (t) => {
  const f = await fixture(t);
  await f.write("entry.mjs", "export const value = 1;");
  f.git(["add", "entry.mjs"]);
  await f.write("entry.mjs", "export const = 1;");
  assert.equal(f.run().status, 0);
  f.git(["rm", "--cached", "--force", "entry.mjs"]);
  assert.equal(f.run().report.checked, 0);
});

test("paths with spaces and CommonJS are checked without shell interpolation", async (t) => {
  const f = await fixture(t);
  await f.write("space name.cjs", "module.exports = 1; return;");
  await f.write("notes.txt", "not JavaScript");
  f.git(["add", "--", "space name.cjs", "notes.txt"]);
  assert.equal(f.run().status, 0);
  assert.equal(f.run().report.checked, 1);
  await f.write("space name.cjs", "export const value = 1;");
  f.git(["add", "--", "space name.cjs"]);
  assert.equal(f.run().status, 1);
});
