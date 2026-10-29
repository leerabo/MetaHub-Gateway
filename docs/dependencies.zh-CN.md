# 依赖清单

仓库按运行时保存依赖声明：

- 生产 Python 服务：`infra/production/requirements.in` 和带哈希的 `requirements.lock`。
- 通用 Python 镜像：`infra/docker/requirements.runtime.txt`。
- Capacity Runner：`infra/docker/requirements.capacity-runner.txt`。
- MCP Sandbox：`infra/docker/requirements.mcp-sandbox.txt`。
- Assurance Supervisor：`infra/docker/requirements.assurance-supervisor.txt`。
- 前端：`console-web/package.json` 和 `console-web/pnpm-lock.yaml`。
- JavaScript 示例：`examples/js/package.json` 和 `package-lock.json`。
- Python 示例：`examples/python/pyproject.toml`。
- 基础设施镜像：`infra/production/images.lock.json`。

生产构建使用带哈希的 Python 锁文件和固定镜像摘要。依赖变更后必须重新生成锁文件、运行完整测试并记录新版本。
