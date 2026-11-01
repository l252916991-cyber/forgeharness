# 电商参考应用本地运维手册

适用范围：`commerce-consultation` application/policy v2 的单机 POSIX/SQLite 内部参考部署。状态：产品化工作树；尚未冻结为新候选。

## 启动与探针

```bash
uv sync --python 3.12 --extra dev --extra cli --frozen
uv run --no-sync python examples/commerce_demo.py --serve --state /ABSOLUTE/PRIVATE/STATE
curl http://127.0.0.1:8765/health/live
curl http://127.0.0.1:8765/health/ready
```

状态目录必须为当前用户私有的 `0700` 目录。服务固定监听回环地址；演示角色不是生产认证，不能暴露到公网或局域网入口。`live` 只说明进程可应答；`ready` 会以只读方式检查商品、资料和会话 SQLite 状态。

正常停止使用前台进程的中断信号，等待 Uvicorn 退出。服务进程持有 `.commerce-service.lock`；备份与服务不能同时运行。

## 备份

先停止服务，再使用一个尚不存在且位于状态目录外的备份目录：

```bash
uv run --no-sync python examples/commerce_state.py backup \
  /ABSOLUTE/PRIVATE/STATE /ABSOLUTE/PRIVATE/BACKUPS/commerce-YYYYMMDD-HHMMSS
uv run --no-sync python examples/commerce_state.py verify \
  /ABSOLUTE/PRIVATE/BACKUPS/commerce-YYYYMMDD-HHMMSS
```

工具使用 SQLite backup API，把已提交的 WAL 内容写进独立数据库副本，并复制 trace、checkpoint 等其余持久文件。运行锁、`.sqlite-wal` 和 `.sqlite-shm` 是瞬态文件，不进入备份。输出的 `backup-manifest.json` 绑定 application v2、原绝对路径、文件成员和 SHA-256。

备份包含会话文本和业务记录，应使用与运行状态相同或更严格的访问控制、保留期限和销毁规则。工具不备份外部附件、真实工单系统、模型服务回执或宿主身份库；接入这些系统后，宿主必须建立协调备份和回执索引。

## 同路径恢复演练

当前只支持在同一机器、原绝对路径、相同 wheel 和配置上恢复。先确认服务已停止，保留或移动旧目录用于人工调查，确保目标路径不存在，然后执行：

```bash
uv run --no-sync python examples/commerce_state.py verify /ABSOLUTE/PRIVATE/BACKUP
uv run --no-sync python examples/commerce_state.py restore \
  /ABSOLUTE/PRIVATE/BACKUP /ABSOLUTE/PRIVATE/STATE
uv run --no-sync python examples/commerce_demo.py --serve --state /ABSOLUTE/PRIVATE/STATE
curl http://127.0.0.1:8765/health/ready
```

恢复后至少核对：历史会话、待审批轮次、已有工单、运行 inspect、trace events，以及相同 request_id 不创建第二张工单。任何远端调用结果未知时继续保持未知状态；恢复、删除或重新启动都不能推断其没有执行。

改变绝对路径、跨机器恢复、跨应用版本接管和 schema 迁移没有在本阶段验收。需要这些能力时建立独立迁移方案和候选验证，不能通过修改 manifest 绕过身份检查。

## 常见故障

| 现象 | 处理 |
| --- | --- |
| `health/live` 不通 | 检查进程、监听端口和启动日志；不要修改状态库尝试“修复” |
| `health/ready` 返回 503 | 停止接收请求，检查状态目录权限、磁盘和 SQLite 文件；从已验证备份恢复前保留原目录 |
| 备份提示 service active | 先正常停止服务；不得删除锁文件后强行复制正在写入的数据库 |
| 轮次等待审批 | 由独立 supervisor 重新认证，检查精确操作后审批；用户本人不能自批 |
| 工单结果未知 | 按原 request_id 查询业务台账；不得换 request_id 盲目重发 |
| 重复请求 payload conflict | 说明同一业务请求标识被用于不同内容；保留证据并由调用方纠正标识管理 |

真实上线前还需替换演示认证、连接真实商品与工单接口、设定监控通知、RTO/RPO、保留期限和责任人。本手册不证明这些外部条件已经完成。
