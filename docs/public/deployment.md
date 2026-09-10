# V1 容器部署

根 `Dockerfile` 分为 `frontend-build`、`python-build` 和最小 Python 3.11
`runtime` 三阶段。Runtime 使用 `rag:rag`，不含 Node、`node_modules`、pip、
setuptools 或 wheel；OCI revision、Compatibility Manifest、锁文件和 SBOM 用于
追溯，普通启动只执行版本范围与 Migration 兼容检查。

根 `compose.yaml` 是默认入口，只含 `app` 与固定版本
`qdrant/qdrant:v1.18.3`。数据分别保存在 `rag_data`、`qdrant_data` 和
`rag_secrets`。Qdrant 只在内部网络可见并使用文件托管 API Key；应用另有受限
egress 网络访问已配置的模型服务。Compose 只启动候选镜像，不从工作树构建；
正式构建必须使用下述 `release.py build` 白名单 context 入口。

## Loopback 与 TLS

默认端口映射为 `127.0.0.1:8088:8088`。Compose 显式设置
`RAG_TRUST_LOOPBACK_HOST_PROXY=true`，用于识别 Docker 网桥转发的 loopback
访问；只有端口仍绑定 loopback 时才可保留该设置。

远程访问必须在受控反向代理终止 HTTPS，并同时配置：

- `RAG_TRUSTED_ORIGINS` 为完整 HTTPS Origin，不使用通配符；
- `RAG_TRUSTED_PROXIES` 为代理的明确 IP；
- 代理覆盖并发送 `X-Forwarded-Proto: https`；
- 取消 loopback 发布代理设置，并按部署网络限制应用监听面。

应用不信任未列出的代理，也不会接受客户端伪造的 forwarded scheme。Qdrant 不应
暴露公网端口。

## 查询数据面与出网边界

首次启动、没有活动 Retrieval Profile 或尚未批准远程操作时，查询使用
`default_local_fallback`。这是本地 deterministic embedding、Exact/FTS、结构检索和
词面 Reranker 的明确数据面，不会因为存在向量槽就显示成真实远程 Dense。来源明确的
定义、目的、职责、责任人、表格和列表仍可由本地 renderer 回答。

远程检索需要已验证并激活的 Retrieval Profile、完整向量覆盖及匹配的 Index Revision；
generation/interpret/rewrite 还需要管理员批准当前活动语料清单、具体 operation、模型、
有效期和累计预算。新文档、新版本、删除、恢复、Revision 或模型变化使旧批准失效，服务
不会自动扩大授权或把配置状态误报成“资料中没有答案”。真实 Provider 的 endpoint、
调用次数、usage 与失败类别进入非敏感数据面和 Trace；Secret、Provider body 与私有正文
不进入日志或默认 Trace。

默认开发与验收 lane 无 Key、无外部调用。需要真实 Provider 时，先在同源管理员控制台
完成连接验证、Profile 激活和语料批准，再在已持久化的预算 campaign 内运行；公开合成
授权不能复用于私有知识库。

## 镜像与验收

```bash
python scripts/release.py build
python scripts/release.py verify
python scripts/release.py acceptance
```

`verify` 检查当前 Git SHA、非 root、构建工具边界、Python/npm 依赖、完整与
可修复漏洞清单、Secret、镜像 SBOM 和许可证清单。`acceptance` 启动两个隔离
Qdrant Server，验证双 Named Vector、故障数据、快照、恢复和重启持久性；它不调用
真实模型服务。

`verify` 每次都会产生新的完整扫描。发布管理员需要在同一不可变扫描上继续审核
时，使用实际生成的路径执行：

```bash
python scripts/release.py review-os-scan \
  --scan artifacts/<本次目录>/trivy-all.json \
  --risk-review release/p11-os-risk-review.json \
  --freshness-policy release/p11-scan-freshness-policy.proposed.json
```

该动作不重扫、不修改原 JSON，也不把旧批准迁移到新扫描。仓库中的 freshness
文件只是 `PROPOSED` 建议；管理员尚未提供有效政策、逐项处置或批准时，命令和
`SECURITY_READY` 都保持 `BLOCKED`。本地 image ID 与本地 RepoDigest 会单独记录，
不会冒充远端 registry manifest digest。

新的扫描需要逐项调查草案时，可只复用旧文件中的技术分析字段：

```bash
python scripts/os_risk_draft.py \
  --scan artifacts/<本次目录>/trivy-all.json \
  --db-metadata artifacts/<本次目录>/trivy-db-metadata.json \
  --previous-review release/p11-os-risk-review.json \
  --output release/p11-os-risk-review.json
```

生成器按 `CVE + package + installed_version` 重建全量清单，保留 source package、
source version、PURL、arch、Target、layer 和原始条目索引，并强制将 owner、
approver、expiry 与 risk acceptance 清空；输出始终为 `UNDER_INVESTIGATION`。

基础镜像和扫描工具在 CI/报告中记录解析后的 Digest。查询功能、公开 holdout 与容器
问答验收的当前状态见 `BLOCKED.md` 和
`docs/public/evaluation-and-quality-claims.md`；OS 风险、CI、真实 Provider 与 Branch
Protection 仍需按各自证据独立判断，不能由离线查询结果代替。
