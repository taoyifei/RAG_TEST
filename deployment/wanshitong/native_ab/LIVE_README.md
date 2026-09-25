# 腾讯原版 B 独立试用实例

2026-09-25 在 `10.242.180.60` 恢复 Tencent/WeKnora v0.8.2，独立 Compose project 为 `wklive-b-20260925`。`10.242.180.54:18391` 是到 `10.242.180.60:18391` 的独立 TCP 转发。用户入口为 <http://10.242.180.54:18391/>。账号和随机密码只保存在服务器受限目录 `identity.private.json` 和用户本机的试用说明中，不写入 Git。

## A/B 标识

| 标识 | 内容 | 当前用途 |
|---|---|---|
| A | 湾事通 Q1 自研问答，基线提交 `dca24a8` | 原 A/B 结果对照；此次未重建或切换 |
| B | Tencent/WeKnora 原版 v0.8.2，源码提交 `3e8b0bf` | 保留运行，供用户直接试用 |

B 使用独立账号、知识库、数据库、Redis、网络、Compose project 和存储卷。60 上 B 的六个容器分别是 `wklive-b-app`、`wklive-b-frontend`、`wklive-b-docreader`、`wklive-b-postgres`、`wklive-b-redis`、`wklive-b-rerank-adapter`。App 只绑定 `127.0.0.1:18390`；前端只绑定 60 的私网 `18391`。它使用已锁定的内网 Qwen3-8B-AWQ、Embedding 和 Reranker 接口，没有替换 8289 或 18288 的镜像、容器及路由。

## 固定输入和实例配置

- 60 上运行根目录：`/data/tyf/wanshitong-weknora-live-20260925`，权限 `0700`。上游 checkout 在其 `upstream/` 下。
- 原版源码归档 SHA-256：`0d30c5415a43674d4d134293dd27ecb1a3903fc70e19f22e3133ab8e9c193239`。
- 15 份原始 DOCX 的 `corpus.lock.json` SHA-256：`779edd3e8a1e9954d4bf60bd56aef27e66a8fe986e863be70a6b5bae2f73a21e`。导入前由 `restore_b_live.py` 逐份核对哈希和字节数。
- B 的知识库为“湾事通原版试用（15份原件）”，问答智能体为“湾事通原版问答试用”。知识库绑定 Qwen Embedding 和 Qwen3-8B-AWQ 摘要模型；智能体绑定同一聊天模型和 Reranker。测试使用原生 quick-answer，`agent_enabled=false`。
- `upstream/config/config.eval.yaml` SHA-256：`9799e3efe64e048de42f8f9b0c0e2f70423d790b0349a9efb47d2ef73245ffcd`。配置包括重排前六条、2680 输出上限、温度 0、关闭思考和固定兜底，不修改上游 Go 问答逻辑。
- 私有运行目录保留 `runtime-before.private.json`、`runtime-after-ui.private.json`、`import.private.json` 和模型冒烟 SSE。它们可能包含内部身份或原文，不提交 Git。

## 启动与检查

在 60 上的 `upstream/` 目录，已存在带原生镜像的本地 Docker image 和受限 `.env`。重启 B 时运行：

```bash
docker compose -p wklive-b-20260925 \
  -f docker-compose.yml -f compose.b.live.override.yaml \
  up -d --no-build --pull never
docker ps --filter name=wklive-b
curl -fsS http://127.0.0.1:18390/health
```

原版三个镜像的已验证 image ID：App `bc8a534799fc`、Docreader `3c36e7e738ba`、UI `83cfd9272e12`。本目录的 `compose.b.live.override.yaml`、`restore_b_live.py` 是实例化配置与原件恢复脚本；服务器副本在 `upstream/`。54 的转发脚本沿用 `/data/tyf/wanshitong-access/tcp_forward.py`，转发日志和 PID 在 `/home/user4a/wklive-b-forward-20260925/`。转发进程独立运行；54 主机重启后需重新启动该转发。

## 已执行的验证

- 15 份文档均显示“已完成”，独立 B 数据库中有 247 个 chunks。
- 通过 54 的前端登录、查看 15 份文档、选中专用智能体并提问，页面返回了带 `DOCX-007.docx` 引用的自然回答。相同主题通过 60 的原生 API 也收到完整 SSE、引用事件和结束事件。该冒烟证明链路可用，不代表 15 份文档整体正确率已经由用户判定。
- 前后快照中受保护的 7 个容器 ID、image ID、运行和健康状态相同。54 的 18288 根页面与 `/kb/` 继续返回 HTTP 200。
- 前端首次生成时控制台出现一次上游 Vue `ReferenceError`，但该次页面回答、引用和完成状态均正常。保留此观察，不据此宣称质量通过。

已归档的完整 A/B 结果另见本目录 `AB_REPORT.md` 及用户本机的 `AB实测结果_20260925`。本实例保留运行，待用户亲自体验后决定是否继续。
