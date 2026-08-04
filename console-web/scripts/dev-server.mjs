/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/scripts/dev-server.mjs
 */

import http from "node:http";
import { createReadStream, existsSync } from "node:fs";
import { stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const port = Number(process.env.METAHUB_CONSOLE_PORT ?? 5173);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
};

const server = http.createServer(async (request, response) => {
  const requestedPath = new URL(request.url ?? "/", `http://localhost:${port}`).pathname;
  const resolvedPath = await resolvePath(requestedPath);

  if (!resolvedPath) {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
    return;
  }

  const ext = path.extname(resolvedPath);
  response.writeHead(200, {
    "content-type": contentTypes[ext] ?? "application/octet-stream",
    "cache-control": "no-store",
  });
  createReadStream(resolvedPath).pipe(response);
});

server.listen(port, () => {
  console.log(`MetaHub console listening on http://localhost:${port}`);
});

/**
 * 说明该脚本函数或测试用例的职责。
 *
 * 修改时需保持可追踪 request_id、权限边界和敏感信息不泄露的覆盖。
 */
async function resolvePath(requestedPath) {
  // 本地开发服务器只允许访问 console-web 目录内的文件，避免路径穿越读到仓库其他文件。
  const normalized = requestedPath === "/" ? "/index.html" : requestedPath;
  const candidate = path.normalize(path.join(root, normalized));
  if (!candidate.startsWith(root) || !existsSync(candidate)) {
    return null;
  }
  const info = await stat(candidate);
  if (info.isDirectory()) {
    return path.join(candidate, "index.html");
  }
  return candidate;
}
