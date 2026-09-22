# WB-08R 阶段 06 回退保留登记

状态：`ROLLBACK_RESOURCES_RETAINED`

记录时间：2026-09-22T21:45:28+08:00

## 旧生产实物

| 资源 | 保留状态 |
| --- | --- |
| 容器 | `wanshitong-app`，ID `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`，正常停止且未删除 |
| 原镜像 | `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306` |
| 原标签 | `rag-test-wanshitong:604ef63` |
| 回退标签 | `rag-test-wanshitong:rollback-pre-wb08r06-20260922-2114` |
| 原数据 | `/data/tyf/wanshitong/data`，未被新镜像 migration 修改 |
| 原日志 | `/data/tyf/wanshitong/logs` |
| 原密钥 | `/data/tyf/wanshitong/secrets`，值不进入文档 |
| 原配置证据 | `/data/tyf/wanshitong-wb08r06-prod-20260922-2114/rollback/` |
| 重启策略 | `unless-stopped`；容器由本次切换显式停止，不占用 8288 |

旧数据停止后的两库 `PRAGMA quick_check` 均为 `ok`。冻结源文件 SHA256：

- `product-traces.sqlite3`：
  `1baf20e287d4ad9c4a51232a7d577c479f667a1a8371c4e0aee8d55cbaff18ef`。
- `universal-rag.sqlite3`：
  `601020def8a5fa0aae279f6f147d8d24d6a29ebd998ab69cad2b53e060fc54f1`。

## 新生产资源

| 资源 | 当前值 |
| --- | --- |
| 应用容器 | `wanshitong-wb08r06-production-app` / `fb65fdaeaec49c161bc6a7e73c9af7eca01cb9c5858341c073c75a64d64abf47` |
| 前缀代理 | `wanshitong-wb08r06-production-prefix-proxy` / `958b1040d543262ed769abc3d8f39966b0723d778631e713b97588ffea76fb14` |
| 镜像 | `sha256:96f1aa48eed69de4a98f48af6f4fa89ebf5b35a6392c30697ee855b105f5ecd9` |
| 工作根 | `/data/tyf/wanshitong-wb08r06-prod-20260922-2114` |
| 内部应用端口 | `127.0.0.1:18288 -> 8088/tcp` |
| 正式入口代理 | `10.242.180.60:8288 -> http://127.0.0.1:18288`，仅代理 `/kb` |

新工作副本在 migration 和生产 Smoke 后继续独立写入。不得用它反向覆盖旧数据，
也不得因旧容器已停止而把旧镜像或旧目录视为过期资源。

## 回退动作

仅在明确需要整体回退时执行：

1. 停止 `wanshitong-wb08r06-production-prefix-proxy`，释放 60:8288。
2. 停止 `wanshitong-wb08r06-production-app`，保留新生产数据和容器供诊断。
3. 确认 8288 无监听者后启动保留的 `wanshitong-app`。
4. 验证 60:8288 的 `/live`、`/ready`，再验证 54:18288 旧版首页与问答。
5. 告知用户：切换后新版本产生的 SSO history、Trace 等仍在新工作副本中，旧版
   页面暂不可见；不得做未经验证的 reverse migration 或全库覆盖。

54 的 18288 TCP 转发未修改，因此回退不需要改 54。8289 测试应用、测试代理、
测试数据和 54:8289 转发继续独立保留，不参与生产回退。
