# V3-03H 接入与隔离验证

## 结论与边界

V3-03H 已在独立开发分支实现来源范围决策与请求级引用句柄，并在 8289 隔离镜像、真实 DOCX、内网 Embedding／Reranker／关闭思考模式的 Qwen3-8B-AWQ 上跑通。正常 SSO 页面形成实时增量和带来源卡的正式回答。代码未发布到 18288；本轮不涉及 Go 分块器、召回通道、PDF 或 OCR。

本轮证明新协议已接入、普通页面可用，并修复了“研发项目命名规范中”被误判为硬文档范围的问题。引用校验只证明来源属于本轮送模材料，**不证明模型事实正确**；无答案问题的错误推断见 [P0](P0-UNSUPPORTED-ANSWER-WITH-VALID-CITATION.md)。

## 身份与实施

- 本方起点：`9b366520453229e5b05770ae350ac9fe27ced69b`；测试版源码：`5d2f723f8a18b04fb7dc75281a289c370c33200e`。
- 上游审查：WeKnora v0.8.0 `1edcd54b43606d9079bb36650efe3f68707a79ea` 与 v0.8.2 `3e8b0bfc80b845b2d4b2ed683994748741450a97`；选择性映射见 [UPSTREAM_MAPPING.md](UPSTREAM_MAPPING.md)。
- 来源范围：`OPEN`、`SOFT_HINT`、`HARD_RESOLVED`、`HARD_UNRESOLVED`；服务端明确选文档及强语法才形成硬范围，解析失败关闭；改写不能改变冻结范围。
- 引用协议：最终送模材料分配请求内 `cN`，正文与标题做 HTML 转义；流式解析私有 `<ref id="cN"/>`，只将本轮已登记句柄映射到公开 `[S1]` 和受控引用卡。未知、伪造、旧式模型输出、残缺标签不形成有效引用；旧公共协议保持原路径。
- 首次 8289 长问 Trace `trace_12f85fc4012d4714a010d7b3c30da3ca` 有 11 个 `FORGED_OR_MALFORMED_TAG`，模型正常 `stop`。按 WeKnora v0.8.2 的通用空白语法修正解码后，同题 Trace `trace_80d55c1d9aa640efa661d1e419985c73` 为 `ANSWERED`、4 个有效引用；仍严格拒绝未知和伪造句柄。原始标签形态未单独保存，不能断言每个标签具体多了哪一个空格。

## 8289 真实验证

四份原始 DOCX 经管理员 API 上传、解析、父子分块与入库均成功；随后另导入一份同名不同路径原件测试歧义。`附件4：研发项目命名规范.docx` 的 SHA256 为 `9b5a0d87e4439c3d2a2f95ce840094482fc14d2cd896fc589803490a78242241`，与此前 P0 记录一致；其余本轮原件见运行清单。测试均只写隔离数据目录和新建索引。

| 验证项 | 实测结果 |
| --- | --- |
| 弱来源表达 | `研发项目命名规范中，研发项目名称由哪些要素组成？` 的 Trace `trace_736dcf7c6fbc4fe68651a6a9b192b578` 显示 `SOFT_HINT`、`allowed_document_count=0`，实际完成 Embedding、Reranker、Qwen 调用并生成有效引用。 |
| 明确指定文档 | 单份原件时 `根据《附件4：研发项目命名规范》` 正常回答；同名原件导入后 Trace `trace_f5c275f64601493088b9f3162f505356` 为 `HARD_UNRESOLVED/AMBIGUOUS`，在检索前返回 `SOURCE_SCOPE_NOT_RESOLVED`。不存在的书名号文档也在检索前失败关闭。 |
| 长问与短问 | 原多阶段长问生成带引用答案；研发工时长问经协议空白修正后带 4 个有效引用；短问带引用。`finish_reason=stop`。不据此宣称完整性或准确率达标。 |
| 普通 SSO 页面 | 通过真实登录，在 54:8289 `/kb/` 发起 4 个独立会话，均显示增量、正式答案和引用卡；名称要素与“30 字以内”答案已对照原始 DOCX。页面未显示私有 `cN`。 |
| 连续追问 | 同一会话追问“那项目名称建议控制在多少字以内？”未形成引用，终态拒绝发布；详情见质量观察。 |
| 停止与断连 | 页面停止后 Trace `trace_27fab426dc8a3aaea7e9231a7c9c1d28` 为 `CANCELLED`，临时正文标为未最终核对。页面在另一条长问流式生成时刷新，随后 Trace `trace_3809f79d8638a84595b1a199f7a33151` 仍为 `ANSWERED`；不能由此断言断连会取消服务端计算，见质量观察。 |

同一 5 文档索引 A/B：旧版 `9b36652` 与新版 `5d2f723` 顺序运行于相同隔离目录、模型配置及 `irev_54dbcdc61a0fc99ad1004a51e6a2f60d`。能进入检索的 4 题 `serving_fingerprint` 相同。旧版的弱来源 P0 在检索前失败，新版有 1 个有效引用；两版的原长问、研发工时长问、名称字数短问均形成带引用答案。两版都错误地把无依据的“火星基地科研项目”审批归给总部科技创新部。逐题结果见 [质量观察](QUALITY_OBSERVATIONS.md)。这组小样本不构成准确率提升的统计证据。

## 门禁与回收

- Python 定向测试：`60 passed`；前端公共 SSE／页面测试：`13 passed`；前端构建成功。改动文件 Ruff、Ruff format、mypy、`git diff --check` 通过。Go 源码未变，固定 `wb-chunker` SHA256 为 `491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`。
- 候选镜像经 Product Asset 自检，部署 render/create guard 均 `ready=true` 且无未分类差异。新建 5 个 Qdrant Collection 与隔离 SQLite 的 5 个版本 ID 完全一致，已逐个删除；原 66 个 Collection 集合未缺失。
- 本轮隔离容器、3 个候选镜像标签、隔离数据目录、临时构建包、浏览器会话与截图、54／60 临时 SSH 公钥及本地私钥均已清理。
- 原 8289 容器 `14c0f9a42c1479ef9c272ef4ff9f0c352c3855d292e9a5c9821f3021c0fdb591` 恢复原名与原镜像 `sha256:ca38110829dd5a1e65589e9571744a1c7342b3b2fc95e94004ba9f7aa9168d06`，状态 `healthy`；18288 容器 `fb65fdaeaec49c161bc6a7e73c9af7eca01cb9c5858341c073c75a64d64abf47` 保持原镜像且 `healthy`。经 54 实际入口读取两处 `/kb/` 均为 HTTP 200。18288 未部署候选代码。
- 本轮未以 GitHub CI 作为已通过门禁；合入后的远端 SHA 另在交付时核对。
