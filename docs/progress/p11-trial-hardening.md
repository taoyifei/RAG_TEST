# P11 受控试用修复与相关内容展示

本轮从 `d0e53e45de03ec65ccedd6267b415c5510af3f49` 继续，在
`codex/p11-trial-hardening` 完成实现。正式 P11 状态由原聚合器生成，
定向传输观察不替代全量质量验收，相关内容展示也不构成风险批准。

**结论：本机既定可信数据范围的技术条件通过，人工风险决定未通过。**
工程修复、部署和有限观察已完成；`CODE_FIXES_READY=true`，
`SECURITY_READY=BLOCKED`、`RETRIEVAL_QUALITY_READY=BLOCKED`、`P11_READY=false`。
其他旧全量运行证据因实现/镜像身份变化而失效的门继续 BLOCKED，不把本次 12 条观察补写为旧全量 PASS。
剩余事项是真实责任人的风险决定，以及后续正式交付所需的安全修复和原质量门证据；
本轮没有扩大试用范围、推进正式发布线或创建 release/tag。

## 当前交付身份

- `90714f7`：白名单传输诊断和兼容预算审计。
- `bfa1f75`：打开归档数据库前的外置信任验证。
- `1487641e9a1b4fa829523b72abd50565fa2f2c3e`：相关内容、API/SDK/UI、缓存与原文访问边界。
- `e0a0851`：正文回读前排除已知删除文档，删除集合隔离缓存。
- `13c80131ff3612c1e9e3d0fd71cf89cf5547eb4c`：恢复检索调试页的候选检查入口，明确未发布用途。
- `8a3cf04f13e1003a1237f08fd4961f85dabaaceb`：`--no-ff` 合回 `codex/p11-final-acceptance`。
- 运行镜像：`sha256:a3354cc9cc81179b8c13c194e1f2ceff2057b46fcba72f25a08d4a1af4adeb82`，
  实际代码为 `13c8013`；报告专用提交不改变运行代码身份。
- 合并提交的 [CI 34075257758](https://github.com/taoyifei/RAG_TEST/actions/runs/34075257758)
  全部 7 个作业通过。机器摘要在 `release/p11-repair-acceptance.json` 的 `trial_hardening`。

## 回答和相关原文

回答资格、原阈值、EvidenceAssembler、NeighborExpander、Confidence 和 Generator
保持原契约。selector 只在正式回答判定之后读取同一请求已有的 canonical 直接候选，
不会追加检索、重排、生成、文档向量化或网络请求。

| 情况 | 用户可见行为 | 正式回答 |
| --- | --- | --- |
| 缺少直接证据，但有可定位的相关原文 | 说明本次依据不足，最多 3 条“相关内容／仅供参考” | 保持拒答、`answer=null` |
| 必要重排实际不可用 | 说明重排服务暂时不可用，候选标记尚未完成相关性复核 | 保持原资格；合格 lexical/exact 仍可回答 |
| 没有足够相关候选 | 说明本次检索未找到足够相关内容，不凑卡片 | 保持拒答 |
| 已具备合格证据 | 原回答与“引用依据” | 不显示拒答相关卡片 |

相关原文仅来自当前 project/KB/active revision，重新检查 metadata/access filters、
canonical 身份、SourceSpan 与删除状态。每 chunk 最多一条、每条最多 240 字、
总计最多 720 字，重复原文去重；无法保证完整行列语义的表格不展示卡片。
原文正文按文本转义，不执行链接、HTML 或文档指令，不进入浏览器持久存储或普通 Trace。
查看原文复用已认证入口，回查当前版本与删除状态，不调用 Provider。

API 与 SDK 的 `include_related_content` 默认 false；产品问答页显式 true。
旧 wire shape 和旧结果反序列化保留。展示开关和 policy 版本进入响应缓存身份，
不改变文档 Embedding/index fingerprint。依赖失败不写稳定结果缓存。
正式引用的片段内偏移与相关原文的 chunk 内偏移分别校验，并验证正文与 canonical 原文一致。
已知软删除文档在读取正文前从各通道剔除，防止仍在索引中的已删除候选阻断合法文档查询；
删除集合进入缓存身份。快照后删除、没有删除记录却缺失或损坏的候选仍失败关闭。
管理员检索页保留原候选详情，使用“检索候选／未发布”标题和独立 DOM 角色，
不会把 `answer=null` 的诊断候选显示为正式引用。

原 60 条已暴露回归的开关对照保持 status、answer、Confidence、正式 Evidence、
原评测指标和调用数一致。相关卡片不计正式 citation，不进入 Generator。
属性缺失/补足、原 positive08、negative03/05/06、standby16–20、表格、范围隔离等
均在既有测试体系验证。离线合成通道不代表真实检索质量。

当前改写仍为 `RuleBasedNormalizer` 的 NFKC/空白处理。
本轮没有新增生成模型或 Query Rewrite operation。后续可选 QueryRewriter 初始 OFF/观察模式，
应另行核对实际 chat/instruct 模型、独立出网授权与预算；保留原问和数字、否定、范围等约束，
失败回退原问，多轮上下文受权限约束，改写不作证据。查询策略身份单独版本化，
使用独立 tuning/holdout 对照 Recall/nDCG、错答/拒答、漂移、P95 与输入输出 Token；本轮不实施。

## 传输诊断与证据边界

旧 18 条真实错误是 Jina query 2、rerank 16；均与本地注入分列。
旧记录缺少具体异常类、HTTP 状态、请求 ID 和直接 case 外键，相关未知字段仍为 UNKNOWN；
按相邻串行查询推定的 case 单独记录，不能当作恢复出的原始字段。
不能据此认定 DNS、TLS、429、额度或供应商事故。

新增记录保留真实异常白名单类别、有证据的 cause/errno/证书原因、单次尝试耗时、
实际 timeout、重试上限和 attempt 关联。日志不保存异常正文、凭据、URL、查询或文档正文。
诊断失败不覆盖业务异常；证书、授权、配置和本地协议错误不盲目重试。
原 timeout、连接池、TLS 验证、`trust_env=False` 和累计预算规则保持。
本地故障 ContextVar、step 和连接生命周期经源码检查与离线作用域测试，
未发现可以证明旧现场根因的证据。

定向观察仅使用原已批准公开集的 positive08 与 16–20，最多 12 个计划观测；
原累计上限与本轮 24 HTTP／12000 estimated（Jina 18／10000、Aliyun 6／2000）取更严格值。
首个观测兼作连接检查；同链路同类真实失败连续两次即停，未执行范围保留 PARTIAL。
开关功能验收全部离线，新增 Provider HTTP 与模型输入/输出 Token 均为 0。

实际完成 12/12 观察：positive08、16–20 的两路均 `ANSWERABLE`，
各自使用计划主/备槽和真实 Provider 重排。24 次远程尝试全为 HTTP_SUCCESS，
没有新增真实传输错误类别，也没有对答案重新执行完整质量评分。
这只能证明本次定向传输恢复；旧 18 条根因仍 UNKNOWN，不能宣称长期网络问题已解决。
备用路的 18 次本地注入拦截与远程尝试单列，不当作真实 Jina 故障。

| 范围 | 新增 HTTP | 新增 estimated | 累计 HTTP | 累计 estimated |
| --- | ---: | ---: | ---: | ---: |
| 全局 | 24 | 8042 | 378 | 113028 |
| Jina | 18 | 7404 | 290 | 103786 |
| Aliyun | 6 | 638 | 88 | 9242 |

known observed 新增 7944、累计 102846；unknown usage 新增 0、累计 21 次，
不能把未知用量计为 0。Jina known 累计 97644／unknown 19；Aliyun 5202／unknown 2。
本地拦截新增 18 次／240 estimated，累计 343 次／24582 estimated。
本轮 HTTP 自限已用完，原 campaign 剩余 61 HTTP／32833 estimated，未重置授权或账本。

首次观察工具因 wheel 镜像不存在源码目录而在调用前退出；独立只读账本证明新增 HTTP=0。
工具随后按已安装模块的真实字节校验同一身份，保留原算法和预算条件，再执行上述 12 条观察。
首次失败、零调用诊断及最后成功收据均保留，未修改产品源码或重建已验证镜像。

## 备份和剩余安全风险

未知归档在任何 SQLite 打开前被拒绝。实际 `create_backup` 创建流程把完整归档 SHA256
写入归档外私有目录；验证和恢复先复制到不可替换的受控临时文件，再检查外部收据，
其后只读该副本。内部 manifest/hash 不能自授信任，旧备份不自动登记。
符号链接、权限错误、路径穿越、外部篡改及校验后替换均拒绝。
原 CLI、维护脚本和升级助手已审计；不存在的备份 API 未新增。
SQLite/伪装数据库作为文档仍由现有格式入口拒绝。

53 条备份相关离线测试及 1 条真实隔离 Qdrant 创建/恢复通过；临时服务已清理。
真实 App 替换前已由候选代码创建可信备份、验证并恢复到隔离空目标：SQLite 状态相同，
6 个 Qdrant 集合及点数相同，隔离目标随后清理。最初的只读恢复目标无法创建 WAL 辅助文件，
维护助手仅将隔离目标挂载改为可写；来源备份及 Secret 保持只读，失败收据保留。

App-only 替换实际完成且健康，替换前后配置、文档、Secret、预算完全相同，Qdrant 未重启。
旧镜像本地元数据存在但无法重新启动，因此从仍在运行的旧容器只读恢复了可启动的回滚镜像
`sha256:f1e693c9315799571b3af08655eb09f279fca54da16c57db44fb018a012f1638`；
业务文件、Python、OS 包和运行资产摘要与原容器相同。旧镜像、备份及失败收据保留。
最终现场再核对 15 项运行、安全和持久数据条件全部通过，检查本身新增 Provider HTTP 为 0。

最终候选的实际 Python `_sqlite3` 加载 `libsqlite3.so.0.8.6`，
SQLite 3.46.1、Debian `3.46.1-7+deb13u1`、amd64。
官方跟踪与隔离容器 APT 刷新未发现同发行版可用修复；不混装 sid/forky、不替换整个基镜像。
这是入口缓解，**不是漏洞已修复**。
来源与受限访问事实保留在 `security-summary.json`，包括
[Debian CVE-2026-11822](https://security-tracker.debian.org/tracker/CVE-2026-11822)、
[CVE-2026-11824](https://security-tracker.debian.org/tracker/CVE-2026-11824) 和
[SQLite 3.53.2](https://sqlite.org/releaselog/3_53_2.html)。

替换后的实际 App 仅发布 `127.0.0.1:8088`，只读根 FS、cap_drop ALL、no-new-privileges；
Qdrant 无宿主发布端口。effective Compose、WSL listener、Windows portproxy 已核对。
本机管理员仍能修改持久卷和信任收据，loopback 也不保证可信管理员永不改配置。

最终镜像扫描完整保留 173 条发现，其中 18 CVE／50 High-Critical 元组与原矩阵一致；
扫描报告 fixable=0 不等于上游没有补丁。不能把一次未加载快照或 non-root 当作不受影响。
没有实际 owner/approver/approval/expiry，风险决定未通过；最长 14 天方案尚未开始生效。
团队、客户、公网、不可信用户与外来备份仍不在本轮允许范围。
技术修复完成不能代替真实风险接受，也不能把本机受控验证写成安全合规或正式 P11 通过。

## 验证证据

原始收据集中在 `artifacts/p11-final/trial-hardening/`，旧失败历史保持原路径。
最终汇总绑定命令退出码、日志 SHA、源码 SHA、实际镜像与运行容器身份；
CI 以实际合并 SHA 为准，不把报告提交冒充运行代码。

- 首轮完整 check：2258 passed；缓存缺陷新增回归先红后绿，专项 14 passed。
- 最终完整 check：2260 passed、88 按既有 local_integration/live_provider 标记排除。
- 源码 `13c8013` 和合并 `8a3cf04` 各自全部 7 个 CI 作业通过。
- smoke 72 passed；product-check 74 passed；product-smoke 6 passed。
- 最终前端 56 passed，lint/typecheck/build 通过；完整 10 项浏览器套件为 7 passed、
  3 项保留原有平台适用条件排除，截图及首轮失败报告均保留。
- Secret 扫描：1337 个源码/前端产物文件通过；Provider 网络调用为 0。
