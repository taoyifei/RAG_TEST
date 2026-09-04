# Provider Credential 安全边界

Credential 与 Provider Connection 分离。支持部署环境托管和数据库加密托管两种来源。

## 部署环境托管

数据库只保存环境变量名。列表接口返回 `configured`，不会读取或返回环境变量值。
Provider 请求边界才解析当前值，所以部署侧轮换无需把 Secret 复制到页面。

## 数据库加密托管

使用 `cryptography` 的 AES-256-GCM。每次写入生成随机 96-bit nonce。AAD 明确绑定
`credential_id`、`provider_type`、`field_name` 和 `key_version`；任一字段变化都会使
认证解密失败。主密钥必须是 32 bytes、0600、非 symlink 普通文件，并独立于 SQLite、
镜像和 Git。

```bash
rag-app init-secrets --output /controlled/path/master-key
```

命令只输出受控路径与 Key ID/fingerprint。数据库列表和 GET 只暴露 source、
configured、masked hint 与 key version，不返回 nonce、密文或完整值。

轮换会增加 key version，并使引用该 Credential 的缓存 Provider client 关闭。旧版本的
验证记录不再算作当前有效验证。Credential ID、版本、显示名和 API Key 都不进入索引
语义 fingerprint。

测试扫描 SQLite、API 响应、模型 repr、日志和 Trace 所在数据库文件，确认合成 Secret
不以明文出现。
