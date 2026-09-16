# 湾事通内网 Demo 快速部署

本目录部署当前 Universal Product Runtime 的湾事通壳层。常驻服务只有
`wanshitong-app` 与 `wanshitong-qdrant`；Embedding、Reranker 和 LLM
均使用部署现场已经验证的内网 HTTP Endpoint。

## 1. 准备目录与镜像

在目标主机以有 Docker 权限的部署管理员执行。所有部署资产必须留在
`/data/tyf/wanshitong`，不要执行 `down`、`rm`、`prune` 或停止其他项目容器。

```bash
sudo install -d -m 0700 /data/tyf/wanshitong/{src,deployment,data,qdrant,secrets,logs,artifacts,corpus,ops}
sudo chown -R 10001:10001 /data/tyf/wanshitong/data
sudo chmod 0700 /data/tyf/wanshitong/secrets
```

把当前目录复制到 `/data/tyf/wanshitong/deployment`，把控制清单复制到
`/data/tyf/wanshitong/src/DOCX_ONLY_MANIFEST_46.json`。先加载已经在受控构建机
生成的应用镜像；镜像中不得包含语料、`.env` 或 Secret。

## 2. 写入现场配置

```bash
cd /data/tyf/wanshitong/deployment
sudo cp .env.example .env
sudo chmod 0600 .env
sudoedit .env
```

必须按现场探测结果填写：

- `RAG_APP_IMAGE`：当前候选或最终 merge SHA 对应的本地镜像 tag；
- `WANSHITONG_PORT`：已确认空闲的宿主机端口；
- `RAG_TRUSTED_ORIGINS`：浏览器实际使用的精确 Origin，并包含本机运维
  Origin；
- 三个模型的 Base URL、模型名、Embedding 维度以及 Reranker 协议和路径；
- 模型需要鉴权时，每个角色只选择 `*_CREDENTIAL_ENV` 或
  `*_API_KEY_FILE` 一种方式。

`.env` 只能存在于目标主机，不得提交 Git、复制进镜像或写入报告。

## 3. 初始化并部署

以下脚本需要读取 0700/0600 Secret，因此用 `sudo` 执行：

```bash
sudo ./generate-secrets.sh /data/tyf/wanshitong/deployment/.env
sudo ./preflight.sh /data/tyf/wanshitong/deployment/.env
sudo ./deploy.sh /data/tyf/wanshitong/deployment/.env
sudo ./smoke.sh /data/tyf/wanshitong/deployment/.env
```

`generate-secrets.sh` 只在四个 Secret 文件全部不存在时调用当前镜像的
`rag-app init-secrets`。若只存在部分文件，它会失败而不会覆盖。`deploy.sh`
先启动 Qdrant，随后真实验证四项模型操作并重复执行配置器验证幂等，最后才
启动 app。

## 4. 导入语料

先导入 Manifest 标记的四个 Pilot 文档：

```bash
sudo python3 import_docx.py \
  --base-url http://127.0.0.1:8288 \
  --bootstrap-token-file /data/tyf/wanshitong/secrets/admin-bootstrap-token \
  --root /data/tyf/wanshitong/corpus \
  --manifest /data/tyf/wanshitong/src/DOCX_ONLY_MANIFEST_46.json \
  --pilot-only \
  --wait \
  --report /data/tyf/wanshitong/ops/pilot-import.json
```

Pilot 的真实问答、引用、History 和 Operational Trace 验收通过后，再导入
全量 46 个 DOCX：

```bash
sudo python3 import_docx.py \
  --base-url http://127.0.0.1:8288 \
  --bootstrap-token-file /data/tyf/wanshitong/secrets/admin-bootstrap-token \
  --root /data/tyf/wanshitong/corpus \
  --manifest /data/tyf/wanshitong/src/DOCX_ONLY_MANIFEST_46.json \
  --resume \
  --wait \
  --report /data/tyf/wanshitong/ops/full-import.json
```

端口不是 8288 时，以上 `--base-url` 必须同步调整。报告只保存路径、SHA、
Job 和可检索状态，不保存 Secret 或文档正文。

日常检查与故障定位见 [OPERATIONS.md](OPERATIONS.md)，使用边界见
[DEMO_LIMITATIONS.md](DEMO_LIMITATIONS.md)。
