/**
 * 验证独立制品文件、锁定依赖、README 命令和浏览器安全边界保持一致。
 */

import assert from "node:assert/strict";
import { readFile, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// 项目根路径只用于扫描 examples/js，测试不得访问或写入其他业务目录。
const PROJECT_ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const PUBLISHED_FILES = [
  ".env.example",
  "README.md",
  "package.json",
  "package-lock.json",
  "src/chat.js",
  "src/client.js",
  "src/config.js",
  "src/errors.js",
  "src/stream.js",
];

/**
 * 读取项目内 UTF-8 文本，统一测试中的路径拼接方式。
 */
async function readProjectFile(relativePath) {
  return readFile(join(PROJECT_ROOT, relativePath), "utf8");
}

test("九个受控发布文件和六个测试文件全部存在", async () => {
  const testFiles = [
    "tests/chat.test.mjs",
    "tests/client.test.mjs",
    "tests/config.test.mjs",
    "tests/errors.test.mjs",
    "tests/project-files.test.mjs",
    "tests/stream.test.mjs",
  ];

  for (const relativePath of [...PUBLISHED_FILES, ...testFiles]) {
    assert.equal((await stat(join(PROJECT_ROOT, relativePath))).isFile(), true);
  }
});

test("package 与 lockfile 固定 openai 6.47.0 和 Node 22 基线", async () => {
  const packageJson = JSON.parse(await readProjectFile("package.json"));
  const lockfile = JSON.parse(await readProjectFile("package-lock.json"));

  assert.equal(packageJson.type, "module");
  assert.equal(packageJson.engines.node, ">=22.0.0");
  assert.equal(packageJson.dependencies.openai, "6.47.0");
  assert.equal(lockfile.packages[""].dependencies.openai, "6.47.0");
  assert.equal(lockfile.packages["node_modules/openai"].version, "6.47.0");
});

test("README、环境模板、脚本与源码入口使用相同名称", async () => {
  const readme = await readProjectFile("README.md");
  const envExample = await readProjectFile(".env.example");
  const packageJson = JSON.parse(await readProjectFile("package.json"));
  const variables = [
    "METAHUB_BASE_URL",
    "METAHUB_VIRTUAL_KEY",
    "METAHUB_CHAT_MODEL",
    "METAHUB_TIMEOUT_SECONDS",
    "METAHUB_MAX_RETRIES",
  ];

  assert.match(readme, /node --env-file=\.env src\/chat\.js --prompt/);
  assert.match(readme, /node --env-file=\.env src\/stream\.js --prompt/);
  assert.equal(packageJson.scripts.chat, "node --env-file=.env src/chat.js");
  assert.equal(packageJson.scripts.stream, "node --env-file=.env src/stream.js");
  assert.ok(variables.every((name) => readme.includes(name)));
  assert.ok(variables.every((name) => envExample.includes(name)));
});

test("环境模板只含明显占位 Key 且不含授权头或可用密钥格式", async () => {
  const envExample = await readProjectFile(".env.example");

  assert.match(envExample, /^METAHUB_VIRTUAL_KEY=your-virtual-key$/m);
  assert.doesNotMatch(envExample, /Authorization:\s*Bearer/i);
  assert.doesNotMatch(envExample, /\bsk-[A-Za-z0-9]/);
  assert.doesNotMatch(envExample, /\bvk_[A-Za-z0-9]/);
});

test("运行时代码不启用浏览器直连且不记录敏感请求对象", async () => {
  const source = (
    await Promise.all(
      ["chat.js", "client.js", "config.js", "errors.js", "stream.js"].map((name) =>
        readProjectFile(`src/${name}`),
      ),
    )
  ).join("\n");

  assert.doesNotMatch(source, /dangerouslyAllowBrowser\s*:/);
  assert.doesNotMatch(source, /console\.(log|error|debug)/);
  assert.match(source, /logLevel:\s*"off"/);
  assert.match(source, /typeof window !== "undefined"/);
  assert.match(source, /\.withResponse\(\)/);
  assert.match(source, /AbortController/);
});

test("项目不分发本地 env、node_modules 或额外发布文件", async () => {
  await assert.rejects(stat(join(PROJECT_ROOT, ".env")), { code: "ENOENT" });
  const readme = await readProjectFile("README.md");

  assert.match(readme, /禁止让浏览器直接连接 Gateway/);
  assert.match(readme, /\.env.*不应提交到 Git/);
  assert.equal(PUBLISHED_FILES.length, 9);
});
