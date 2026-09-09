/** Check exactly the JavaScript bytes about to be committed, not the worktree. */
import { execFileSync } from "node:child_process";

const files = execFileSync("git", ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
  .toString("utf8").split("\0").filter((file) => /\.(?:mjs|cjs|js)$/u.test(file));
const failures = [];
for (const file of files) {
  const source = execFileSync("git", ["show", ":" + file]);
  try {
    execFileSync(process.execPath, ["--input-type=" + (file.endsWith(".cjs") ? "commonjs" : "module"), "--check"], {
      input: source, stdio: ["pipe", "pipe", "pipe"],
    });
  } catch {
    failures.push(file);
  }
}
console.log(JSON.stringify({ scope: "git index JavaScript syntax only", checked: files.length, failures }));
if (failures.length) process.exitCode = 1;
