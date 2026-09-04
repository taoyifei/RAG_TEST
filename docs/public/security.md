# 安全配置

## 浏览器与 TLS

Product Runtime 的管理员页面使用 HttpOnly、SameSite=Lax Cookie，会话写操作同时
要求内存中的 CSRF Token。非 loopback 主机名必须使用 HTTPS；反向代理只有其源 IP
明确列入 `RAG_TRUSTED_PROXIES` 后，应用才接受该连接的
`X-Forwarded-Proto`。`RAG_TRUSTED_ORIGINS` 必须列出完整 `http://` 或
`https://` Origin，不接受通配符、路径、凭据或 query。

每个响应设置 CSP、`frame-ancestors 'none'`、`X-Content-Type-Options:
nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 与受限的
Permissions Policy。HTTPS 响应额外设置 HSTS。API 响应禁止缓存。

默认固定窗口限流为每客户端每 60 秒：Provider Test 5 次、上传 10 次、查询 60
次；Bootstrap 登录独立限制为 5 次失败。外部 API Token 必须满足 scope、项目/知识库范围、过期时间
与吊销状态。

## Secret

页面托管 Provider Key 只以 AES-256-GCM 密文进入 SQLite，完整值不进入读取 API、
日志、Trace、浏览器存储或普通备份。主密钥、首次管理员口令与 Qdrant API Key 使用
独立 0600 文件；主密钥不进入普通备份包，丢失后页面托管凭据无法恢复。

默认 Compose 端口只绑定 `127.0.0.1`。其中
`RAG_TRUST_LOOPBACK_HOST_PROXY=true` 只用于 Docker 网桥转发的本机请求，不能与
公网端口绑定组合使用。非 loopback 部署必须通过明确可信代理终止 HTTPS。

发布前运行：

```bash
python scripts/secret_scan.py --docker-image docx-rag:v1-candidate
```

可用 `--path` 重复加入数据目录或备份包。扫描器只输出规则和位置，不输出命中的
Secret 原文。CI 还会运行独立 secret-scan 作业；任何发现都阻止发布。

`python scripts/release.py verify` 还会运行 pip/npm 审计、Trivy 完整漏洞清单、
可修复 High/Critical 阻断门、CycloneDX SBOM 和确定性许可证清单。完整扫描中的
无修复版本问题必须记录为剩余风险；`--ignore-unfixed` 只定义当前可修复阻断门，
不能把完整风险清单说成零漏洞。
