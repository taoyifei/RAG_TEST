# 复制现有公共资料到 8289

此脚本只用于当前湾小度公共库的 99 份资料迁移。源库包含原有 49 份和新增报账资料 50 份；本次把全部 99 份作为新版公共资料。

生产源固定为 `wst_weknora_candidate_20260925-postgres-1` 中的公共库 `e6e800f1-f27d-41a5-9622-4e3ebebb7e73`，租户 `10000`。源连接通过 PostgreSQL 参数强制只读，导出使用 `REPEATABLE READ READ ONLY` 同一快照。脚本不调用解析、Embedding 或回答模型，不更新生产资料和文件。

## 先准备新版原生环境

- 独立 Compose project 为 `wxd_v2_083_8289`，PostgreSQL 已完成 0.8.3 初始化。
- 在新版原生服务中建立目标租户、管理员和**空的公共知识库**；目标库绑定自己的 local storage backend。
- 目标公共库使用与生产相同的 Embedding 模型和维度。默认模型 ID 保持 `wkab-qwen3-embedding-0-6b`；如模型 ID 改变，只有实际权重与维度一致时才传 `--model-map-json`。
- 模型服务由新版配置重新建立。脚本不复制 `models`、用户、会话、API Key、存储凭证或生产密钥。
- 在 60 服务器运行。原件来源目录固定 `/srv/wanshitong-data/files`；目标必须明确传入 `/srv/wanshitong-data/wxd-v2-test/files`，并与新 Compose 的 `native-files` 卷匹配。

## 先生成清单

将下列占位值替换为已创建的目标原生用户和知识库 ID。目标租户 ID 取真实值，不根据示例猜测。

```sh
python3 deployment/wanshitong-v2/clone_public_data.py \
  --source-tenant-id 10000 \
  --target-tenant-id TARGET_TENANT_ID \
  --target-kb-id TARGET_PUBLIC_KB_ID \
  --target-owner-id TARGET_NATIVE_ADMIN_USER_ID \
  --target-container wxd_v2_083_8289-postgres-1 \
  --target-files /srv/wanshitong-data/wxd-v2-test/files
```

默认仅在测试数据根目录下生成 `public-migration/snapshot.ndjson`、`import.sql` 和 `manifest.json`，并读取、校验源文件；不导入数据库或复制目标文件。清单记录各表数量、每个文件的相对路径、字节数和 SHA256。快照包含院内资料正文和索引，应保存在当前受限服务器目录，不提交 Git。

核对清单后，使用相同参数并增加 `--apply`。复制先逐个验证文件，恢复测试 app 对文件卷的写权限，随后在目标 PostgreSQL 单事务内导入。目标公共库非空时拒绝导入；不会覆盖已有资料。数据库失败会回滚，已经复制且哈希相同的测试文件可供修复后重试。成功后 `manifest.json` 写入 `applied=true` 和全部迁移表的实测数量。

## 现场容器运行方式

60 服务器的 `/srv/wanshitong-data` 父目录是 root 所有且权限为 `0700`，不能直接用宿主机 `user4a` 读取原件。现场使用已缓存的 Python 镜像运行工具，源文件和 release 均只读挂载，只有独立测试数据根目录可写；容器通过 Docker socket 调用目标容器内的 psql。

下面记录本次真实迁移使用的镜像、路径和目标身份。**当前目标库已导入，勿重复执行；仅作为空测试库恢复时的参考。** 首次预检应去掉最后的 `--apply`，并根据新建空库核对目标 ID。

```sh
docker run --rm --pull=never --network none --user 0 \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/wanshitong-data/files:/srv/wanshitong-data/files:ro \
  -v /srv/wanshitong-data/wxd-v2-test:/srv/wanshitong-data/wxd-v2-test \
  -v /data/tyf/wxd-v2-20261029/source:/release:ro \
  sha256:a36c24f9cbdf4fd0f52d67f0823eeac19c2028c637cecc392d97f980d4fec56b \
  python /release/deployment/wanshitong-v2/clone_public_data.py \
  --source-tenant-id 10000 \
  --target-tenant-id 10000 \
  --target-kb-id a9d59b6e-5443-4a8f-a289-695d7231b6e6 \
  --target-owner-id 5fb8eeec-4bc4-4f6c-b088-25ab27abddc7 \
  --target-container wxd_v2_083_8289-postgres-1 \
  --target-files /srv/wanshitong-data/wxd-v2-test/files \
  --apply
```

## 复制后的文件所有权

迁移工具以 root 创建目录。若 app 已启动，复制形成的 `10000/` 等目录可能仍属于 root：旧原件能读，但 `appuser` 上传新文件会报 `mkdir permission denied`。

脚本在 `_copy_files` 完成后、数据库导入前自动处理这个问题。它只允许固定容器 `wxd_v2_083_8289-app-1`，核对 Compose project 为 `wxd_v2_083_8289`、service 为 `app`，并确认 `/data/files` 是可写卷 `wxd_v2_083_8289_native-files`。全部匹配后，仅在该测试容器内以 root 执行 `chown -R appuser:appuser /data/files`。任何身份或挂载不匹配都会停止，不会对生产目录执行权限修改。

## 2026-10-29 现场结果

由主执行者在 60 完成首次导入：99 份文档、801 个分块、870 条 Embedding、309 个资源及 309 条资源绑定、309 个实际文件。全部表数量和文件哈希核对通过，生产源保持只读。随后通过原生 `reuse_vectors` 将 1 份人力制度移至公开人力库，公共资料合计仍为 99 份。

## 保留与调整

- 保留文档、分块、标签、资源 ID 和 `resource://handle`，保留文件相对 key 与原目录 `folder_path`。
- 复制 `knowledges`、`chunks`、`embeddings`、标签及资料绑定的 `resources/resource_bindings`。资源范围只包含这些公开资料拥有或正文引用的对象。
- `tenant_id`、`knowledge_base_id` 和编辑者/创建者改为目标身份。`storage://旧backend/` 替换为目标 backend；按原生公式 `SHA256(完整 physical_path)` 重新计算 `location_hash`。
- 文件复制为独立字节副本；不与生产文件建立硬链接。原件和图片分别校验，避免迁移后只有文字没有图片。
- 显式列导入兼容新字段默认值；重新生成分块/标签顺序号与 Embedding 表自增 ID，保留语义对象 ID 和向量。
- 公开人力制度仍保留原目录，完成总量校验后再由新版原生 `reuse_vectors` 功能移至公开人力库；这一步不在脚本中执行。

本脚本另经过本机隔离 PostgreSQL 17 + pgvector 小数据演练：验证原始向量、中文和转义字符、资源路径替换、新字段默认值、非空目标拒绝、只读源连接和独立文件副本。上面的现场记录证明资料复制与哈希核对完成；真实上传、检索和问答验收以部署验收记录为准。
