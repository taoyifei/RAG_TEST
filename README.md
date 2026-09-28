# 湾事通（Wanshitong）

湾事通是中国移动（广东）湾区研究院（GMCII）的内部知识库问答系统。本分支 `wanshitong-stable` 保留湾事通的用户界面和 RDMS 登录，使用 [WeKnora](https://github.com/Tencent/WeKnora) 处理文档、检索和回答。

## 访问

- **员工问答**：[正式入口](http://10.242.180.54:18288/)；使用原有 RDMS 账号登录，可提问、查看流式回答与引用、浏览历史并反馈。
- **管理后台**：[湾事通管理后台](http://10.242.180.54:18288/kb/admin/)；管理员用独立口令管理知识资料、用户端设置、模型与解析、使用记录及诊断。口令只保存在服务器 Secret 中，不写入仓库。

当前发布状态与测试入口是否开放，以[发布门禁](deployment/wanshitong-stable/PILOT-RELEASE-GATE.md)为准。

## 工作方式

```text
员工 → 湾事通 React 页面 → 身份与会话网关 → WeKnora
                                             ├─ 文档解析、分块与检索
                                             └─ 原生流式回答与引用

管理员 → 湾事通 Vue 后台 → 同一网关 → 同一套 WeKnora 配置与资料
```

网关负责身份、权限、会话和流式传输；问答算法与文档处理由 WeKnora 完成。管理员在“用户端设置”发布的知识库范围和回答配置供员工的新问答使用。管理后台只管理与复核，不提供另一套聊天入口。

## 管理员常用操作

1. **上传资料**：进入“知识资料”→目标知识库→“添加文档”→“上传文档”，等待解析完成并检查分块。
2. **控制用户端**：在“用户端设置”选择资料范围、模型和回答参数，保存后确认配置状态为 `ACTIVE`。上传到未发布的知识库，不会自动进入员工问答范围。
3. **排查问题**：在“使用记录”查看提问与反馈，在“系统与诊断”查看运行状态和 Trace。

新文件建议先在私有知识库验证解析结果，再发布到员工可用范围。图片、扫描 PDF 和含图 DOCX 的效果取决于当前解析配置与文件内容。

## 代码位置

| 路径 | 用途 |
| --- | --- |
| [`frontend/src/public/`](frontend/src/public/) | 员工端 React 页面 |
| [`src/wanshitong_gateway/`](src/wanshitong_gateway/) | 湾事通身份、会话、配置与 WeKnora 接入 |
| [`vendor/weknora/frontend/src/views/wanshitong/`](vendor/weknora/frontend/src/views/wanshitong/) | 白标 Vue 管理页面 |
| [`vendor/weknora/`](vendor/weknora/) | 固定版本的 WeKnora 源码 |
| [`deployment/wanshitong-stable/`](deployment/wanshitong-stable/) | 部署配置、验收与回退说明 |

## 开发与运维

- [部署与配置](deployment/wanshitong-stable/README.md)：服务、镜像、Secret、OCR 与数据盘。
- [发布门禁](deployment/wanshitong-stable/PILOT-RELEASE-GATE.md)：已验证能力和剩余限制。
- [切流与回退](deployment/wanshitong-stable/PILOT-CUTOVER-ROLLBACK.md)：现行链路及故障处理。
- [旧 Universal RAG 运行指引](docs/public/legacy-runtime.md)：仓库中保留的旧实现；当前湾事通公共问答不走这条链路。

部署需要内网模型、RDMS 和服务器 Secret。修改分支代码不会自动更新正式环境；发布前应按部署文档核对实际镜像、配置与入口。
