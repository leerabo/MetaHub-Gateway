# 升级与回滚

升级是分阶段、只向前的切换。控制器会验证发布清单，拒绝数据库镜像变化，在旧版本继续服务时构建新服务，运行预检，创建并验证加密备份，执行迁移，最后通过就绪检查后启动新版本。

```bash
sudo scripts/deploy/metahubctl.sh upgrade /opt/metahub-state \
  /opt/metahub-releases/v2.1.0 v2.1.0
```

发布目录必须有与版本匹配的 `release-manifest.json` 和镜像锁定文件。旧版本、备份和 `recovery.json` 在业务验收结束前都要保留。迁移不自动降级；中断时使用新的状态目录执行 `recover`，不要删除 Docker 卷或手工修改数据库。
