# WeKnora V3-03 正常页面接入与隔离实测

## 范围和代码身份

- 基线：Q1 `9590fffdaa4116f08ea266832b7f8fe78ba78393`；上游参考：WeKnora v0.8.0 固定提交 `1edcd54b43606d9079bb36650efe3f68707a79ea`。
- 本轮候选提交依次为 `f82d905`（公共自然流与会话）、`891a16f`（镜像携带固定分块器）、`04c6bc7`（终态 DTO 可选字段）、`5fce5dc22a993655bc9f54581f6338c569e2affb`（显式停止）。最后一个 SHA 为真实测试镜像的源码身份。
- 保留旧公共 SSE 默认协议；仅候选部署启用 `wanshitong-natural-sse-v1`。正常 SSO 页面使用候选链路，临时增量与最终带引用答案分开呈现。自然轮次保存到独立加密会话表，不伪造旧 Claim 校验结果。

## 执行过的验证

| 检查 | 结果 |
|---|---|
| 最终源码定向 Python 测试 | `test_natural_public_api.py`、`test_public_chat_api.py`、`test_public_stream.py`、`test_public_session.py`、`test_natural_conversation.py`：36 passed |
| 最终源码前端测试与构建 | `usePublicChat.test.tsx`、`publicSse.test.ts`：13 passed；`npm run build` 通过（Vite 大包提示，不是构建失败） |
| 静态检查 | 改动 Python 文件 Ruff、mypy 和 `git diff --check` 通过 |
| 8289 镜像 | 以原 8289 镜像为基底构建独立候选；`SOURCE_REVISION=5fce5dc...`，43 个 Product Asset 自检通过；部署 render/create guard 均 `ready=true` 且无未分类差异 |
| 原文入库 | 从已有内网语料目录选取 4 份原始 DOCX，独立目录导入结果为 `selected=4, retrievable=4, failed=0`；没有回写 8289 原数据 |
| 普通页面 | SSO 登录后真实提问；“院内询价数量标准是多少家供应商？”发布含 `[S1]` 的“3 家及以上”答案；同会话追问“这些供应商需要具备什么资格能力？”发布 3 段引用答案；刷新后两轮由服务端历史恢复 |
| 真实增量、停止 | 长问题在页面逐步显示临时正文，停止请求返回 `cancelled=true`；`trace_b3def18deec6fe027787bc816741444a` 最终为 `CANCELLED`，没有成功答案。停止前仅 Abort 的实测不能保证服务端收到取消，因此加入显式停止接口 |
| 引用和反馈 | 真实页面展开 3 段 DOCX 来源；较早的 `04c6bc7` 候选镜像中通过受控接口下载原 DOCX，其 SHA256 与原件一致。最终镜像中点击“有帮助”，对应 Trace 的 `feedback_useful=1` |
| 并发 | 最终镜像在同一登录身份下并发 4 个独立会话，4 个 HTTP 200、4 个 `final`、每路 24 个真实 `answer_delta`；同一会话同时提交 2 轮，均 HTTP 200、`final`，Trace 合计 6 个 `ANSWERED` |
| 协议和错误隔离 | 旧协议相关定向测试通过；只导入 4 份资料时，资料外问题返回 `CITATION_MISSING`，未把草稿发布为答案 |

这些测试证明 V3-03 的正常页面链路可用。4 份资料及少量问题不能用于估算真实语料准确率、召回率或生产并发性能。

## 发现的问题

- `04c6bc7` 候选实测中，3 次长问题在约 74–75 秒后以 `PROVIDER_INVALID_RESPONSE` 结束，见 [P0 记录](P0-LONG-QUERY-PROVIDER_INVALID_RESPONSE.md)。该问题没有被计作成功答案；最后一轮修复聚焦停止传播，未把这类长问题误记为已修好。
- 仅 4 份测试 DOCX 的索引无法覆盖正常推荐问题；有一次请求形成 `CITATION_MISSING`。这是语料覆盖观察，不代表完整索引表现。

## 隔离回收

- 60 服务器原 8289 容器 `14c0f9a42c1479ef9c272ef4ff9f0c352c3855d292e9a5c9821f3021c0fdb591` 已恢复原名 `wanshitong-sso-candidate-app` 和原镜像，状态 `healthy`。候选容器、4 个候选镜像标签、独立目录及 60/54 的传输包已清理。
- 本轮独立索引对应的 4 个 Qdrant Collection `irev_af4574b8a31e2f3f9f3f726449f2ad48`、`irev_6c7a6e7009ca7bee725aed81cbfdc567`、`irev_b9db937d564d2995dac3b1fa4cd3336f`、`irev_ae679f46379fcb52c2b837b682c43378` 已逐一删除并确认均不在 Collection 列表中。
- 本地 4 份临时镜像构建上下文、压缩包、前端依赖符号链接和测试下载文件已清理；浏览器测试会话已关闭。
- 生产 18288 容器 `fb65fdaeaec49c161bc6a7e73c9af7eca01cb9c5858341c073c75a64d64abf47` 未修改且健康。最终经 54 代理访问 8289 与 18288 的 `/kb/` 均为 HTTP 200。**没有向 18288 发布候选代码。**

Git 合入与远端核对结果在交付时另行记录；本报告不把未执行的 CI 当成通过。
