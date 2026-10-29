/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/scripts/lint.mjs
 */

import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const files = await collectFiles(root, [".js", ".mjs", ".css", ".html"]);
const failures = [];

for (const file of files) {
  const text = await readFile(file, "utf8");
  if (text.includes("\t")) {
    failures.push(`${file}: contains tab indentation`);
  }
  if (!text.endsWith("\n")) {
    failures.push(`${file}: missing trailing newline`);
  }
}

if (failures.length > 0) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log(`console lint ok (${files.length} files)`);

/**
 * 说明该脚本函数或测试用例的职责。
 *
 * 修改时需保持可追踪 request_id、权限边界和敏感信息不泄露的覆盖。
 */
async function collectFiles(directory, extensions) {
  const entries = await readdir(directory, { withFileTypes: true });
  const result = [];
  for (const entry of entries) {
    if (entry.name === "node_modules") {
      continue;
    }
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      result.push(...(await collectFiles(fullPath, extensions)));
    } else if (extensions.includes(path.extname(entry.name))) {
      result.push(fullPath);
    }
  }
  return result;
}
