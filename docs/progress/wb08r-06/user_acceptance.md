# WB-08R 阶段 06 用户验收闸口

状态：`USER_APPROVED`

确认时间：2026-09-22（生产切换指令所在会话）

## 待确认的唯一候选

- 试用地址：`http://10.242.180.54:8289/kb/`
- 源码 revision：`d814f8d58d89418ced99aefe470fff58ee4b6b6c`
- 镜像 ID：`sha256:96f1aa48eed69de4a98f48af6f4fa89ebf5b35a6392c30697ee855b105f5ecd9`
- 产品资产 manifest：`933c8ad4dfccaa2226047b311808ac9ddd6f30cc2c65fcf024cafa861bae1468`

用户随后明确要求“把 8289 替换当前的 8288”，同时要求保留之前生产镜像，并将
阶段分支合入 `wanshitong` 分支后打标签。该指令与下方确认模板语义等价，且指向
当时仍在线的唯一 8289 候选，因此记录 `USER_APPROVED=true`。

## 本次确认关联范围

- 批准镜像：`sha256:96f1aa48eed69de4a98f48af6f4fa89ebf5b35a6392c30697ee855b105f5ecd9`。
- 正式入口：`http://10.242.180.54:18288/kb/`。
- 保留原生产容器、镜像、配置引用、密钥和数据，不删除、不 prune。
- 新生产从原生产最终数据创建独立工作副本，不直接使用 8289 试用数据库。
- 8289 继续保留为独立测试环境。

## 确认后的执行结果

06-E 已执行并通过最小生产 Smoke，状态进入
`RELEASED_WITH_ROLLBACK_RETAINED`。具体容器、数据副本、验证和回退位置见
`cutover_report.md`、`release_identity.json` 与 `rollback_retention.md`。
