# V1 容器部署

根 `Dockerfile` 分为 `frontend-build`、`python-build` 和最小 Python 3.11
`runtime` 三阶段。Runtime 使用 `rag:rag`，不含 Node、`node_modules`、pip、
setuptools 或 wheel；OCI revision、Compatibility Manifest、锁文件和 SBOM 用于
追溯，普通启动只执行版本范围与 Migration 兼容检查。

根 `compose.yaml` 是默认入口，只含 `app` 与固定版本
`qdrant/qdrant:v1.18.3`。数据分别保存在 `rag_data`、`qdrant_data` 和
`rag_secrets`。Qdrant 只在内部网络可见并使用文件托管 API Key；应用另有受限
egress 网络访问已配置的模型服务。

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

基础镜像和扫描工具在 CI/报告中记录解析后的 Digest。发布前仍需检查
`docs/progress/phase-11.md` 中未修复漏洞、CI、Live 与 Branch Protection 状态。
