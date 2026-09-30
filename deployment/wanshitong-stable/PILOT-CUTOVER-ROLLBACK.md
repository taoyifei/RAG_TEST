# 18288 受控切流与回退（更新于 2026-09-30）

本手册对应正式地址 `http://10.242.180.54:18288/` 和 `https://devmng.nat200.top/kb/`。54:18288 转发本身未改。2026-09-30 首次尝试接入“大家常问”候选时，公网 SSO 首跳报 `AUTH_ENTRY_INVALID`，随即回退；修复受信代理配置并完成独立 8289 验收后，经用户授权再次切流。**当前正式入口运行“湾小度”**：60:8288 由 `wst_pilot_prefix_proxy_popular_20260930` 提供公共根别名，转到本机 `127.0.0.1:18294` 的 `wst_pilot_production_edge_brand_20260930`，再进入生产网关和原生 WeKnora。上一版代理 `wst_pilot_prefix_proxy_layout_20260928` 已停止，上一版 edge `wst_pilot_production_edge_citation_live_r2_20260928` 仍在 60:18293 供回退；更早的 60:18291 前端也保留。不要把 60:8288 与对外地址 54:18288 混淆。

旧 `wanshitong-wb08r-root-production-prefix-proxy` 已停止，旧 `wanshitong-wb08r-root-production-app` 及其数据、镜像仍保留。54:8289 测试转发仍指向 60:8289 独立网络上的 `wst_pilot_brand_edge_8289_20260930` 和 `wst_pilot_brand_gateway_8289_20260930`；它们不连接生产 WeKnora。原 60:18289 `wanshitong-wb08r06-prefix-proxy` 和旧候选 `edge-1` 仍暂停。**当前生产仍依赖原候选项目中的 gateway、原生 app、PostgreSQL、Redis、Docreader、OCR 等核心容器，不得随测试入口一起停止。**

## 当前配置与备份

- 60 上的部署目录为 `/data/tyf/wanshitong-weknora-stable-candidate-20260925/wanshitong-stable`。其忽略的 `.env` 已持久化正式 Origin、正式 RDMS client ID 与回调配置，验证地址使用已在 8289 成功的 `192.168.48.1:42100`。正式专用 Secret 位于同级 `pilot-20260927/secrets-production-pilot/`，绝不写入 Git。
- 管理入口为 `http://10.242.180.54:18288/kb/admin/`。生产管理员口令在 60 上执行 `docker exec wst_weknora_candidate_20260925-gateway-1 cat /run/wst-secrets/gateway_admin_bootstrap_token` 读取；此次切流后已轮换，旧 8289 口令不再有效。
- 受限快照目录是 `/data/tyf/wanshitong-weknora-stable-candidate-20260925/pilot-20260927/backup/`。`SHA256SUMS` 对应冻结写入时的 PostgreSQL、原生文件、网关 SQLite、Redis、原候选密钥及配置；`SHA256SUMS-PRODUCTION` 对应轮换后的正式配置、密钥和代理容器配置。已验证哈希、`pg_restore -l` 与 SQLite 完整性；**尚未对本次快照做隔离恢复**。目录与 Secret 文件须保留并限制读取。
- 本次切流前另建受限目录 `pilot-20260927/backup/brand-cutover-20260930T0732Z`，保留变更前 `.env`、Docker 状态、网关 SQLite 一致快照及 `SHA256SUMS`；快照完整性为 `ok`，哈希校验通过。它是本次网关和入口变更的回退依据，不替代 PostgreSQL/原生文件的配套备份。
- 当前 8289 edge 和网关已放在独立网络；网关使用临时 SQLite 和测试 SSO 身份，没有生产原生引擎连接。2026-09-30 已完成真实 RDMS 登录与回调，并把生产已审核题面的 16 条只读快照复制到该临时库，检查了真实会话下的列表和分类。点击后的聊天 UI 使用模拟问答 API；没有真实问答或生产资料验收。若要做完整集成测试，须从一致快照恢复独立原生引擎、卷与网络，不能把 8289 重新接到生产网关和数据。

## 2026-09-30 常问候选的发布前检查

公网请求到达生产 edge 时，NAT200 转成 HTTP；edge 只在登记的公网 Host 上补 `X-Forwarded-Proto: https`。首次失败时，生产网关 `RAG_TRUSTED_PROXIES` 只含上一版 edge 的 `192.168.48.5`，新版 edge 在共享前端网络中的 IP 为 `192.168.48.3`。网关当时忽略了来自未受信 IP 的转发协议，因而把公网请求判成 HTTP 并拒绝 SSO 入口。此次切流前在保留旧 IP 的同时加入新 IP，重建网关；旧、新 edge 的 HTTPS 回调预检都已通过。容器重建后 IP 可能变化，应重新预检，不能把现场 IP 当成固定配置。

在 60 主机从本目录运行以下**只读**检查。检查命中时只访问 SSO 首跳，不进入 RDMS 登录或提交验证码；不要打印网关完整环境变量或 Secret。

```sh
python3 preflight-edge-sso.py \
  wst_pilot_production_edge_brand_20260930 \
  wst_weknora_candidate_20260925-gateway-1 \
  --network wst_weknora_candidate_20260925_front \
  --edge-url http://127.0.0.1:18294 \
  --origin https://devmng.nat200.top
```

2026-09-30 再次切流实测：生产网关健康，上一版与“湾小度” edge 的公网 HTTPS SSO 预检均为 `PASS`，回调为 `https://devmng.nat200.top/kb/sso/callback`。公共应用重新发布为 `ACTIVE` 修订版 6；七类意图提示词与主提示词首句均为“湾小度”，其余原生回答配置与修订版 5 一致。正式公网域名已用真实 RDMS 验证码登录并进入湾小度首页；内网正式入口完成身份问答、知识库问答与引用片段打开。管理登录页在正式公网域名显示“湾小度 · 管理后台”。

## 运行检查与故障处理

1. 从正式地址检查 `/`、`/kb/`、`/kb/admin/`；从全新浏览器完成 RDMS 验证码登录，确认用户名、提问、流式结束、引用、刷新后历史和管理后台。只看到 HTTP 200 不算完成验收。
2. 先区分 54 转发、60:8288 根别名代理、60:18294 当前前端、网关、原生 app 和 RDMS 验证服务，再做有限修复和复测。若正式入口持续不可用或影响现有用户，应及时切回已知可用链路。
3. 若需退回上一版页面，在 60 上停止 `wst_pilot_prefix_proxy_popular_20260930`，启动保留的 `wst_pilot_prefix_proxy_layout_20260928`；它指向运行中的 60:18293 上一版前端。若需同时恢复上一版模型身份与网关代码，先将 `.env` 恢复为本次受限备份中的版本，并使用现行四份 Compose 文件仅重建 `gateway`；公共应用绑定已从修订版 5 更新到 6，须在停流后从本次网关 SQLite 一致快照恢复绑定或以旧代码重新发布旧提示词，并核对历史/会话影响，不能只切前端就宣称完全回退。恢复后须重做正式地址 SSO 与问答烟测。若还需退回 60:18291 前端，再停止占用 60:8288 的代理，启动 `wst_pilot_prefix_proxy_isolated_20260927`。不要同时启动两个占用 60:8288 的代理。
4. 需要恢复旧生产版本时，停止当前 60:8288 代理，启动保留的 `wanshitong-wb08r-root-production-prefix-proxy`，旧应用仍在 60:18290。**已知旧生产在切流前就因 `172.24.7.172:21000` 验证地址从 60 连接超时而无法完成登录**；直接切回旧代理只能恢复旧页面，不能宣称登录可用，必须先处理旧 SSO 验证地址并实际复测。不要删除或覆盖任何一侧的数据与镜像。
5. 轮换正式口令或重建网关时，在部署目录用当前 `.env`、`compose.engine.yaml`、`compose.wrapper.yaml`、`compose.ocr.yaml` 和 `compose.storage-nvme.yaml` 仅重建 `gateway`；重建会让旧管理会话失效，完成后从正式地址用新口令重新登录。不要从冻结的 `candidate-original.env` 直接重建生产网关。

## 切流前准备（历史记录，已执行）

1. G1 双用户隔离已在 8289 完成，结果见[发布门禁](PILOT-RELEASE-GATE.md)；公共问答 4 运行、8 等待及超额 429 也已验证。继续核对 G3 中同一模型的后台任务和其他应用调用，并核对公开资料、Agent/模型配置与管理员权限。
2. 决定旧生产历史/反馈是否迁移，明确新栈数据保留期与可读取方式。旧版记录入口已从新页面移除，但旧生产数据没有删除；不要把 UI 删除当作数据迁移决策。
3. 在 60 上保存本轮候选的镜像 ID、Compose 配置、OCR 覆盖配置及密钥位置。切流窗口开始时冻结候选新增写入与导入，重取配套 PostgreSQL、文件卷、网关 SQLite、配置和密钥快照并校验；此前 `backup/pilot-preflight-20260926` 是隔离恢复证据，不是切流时的原子快照。
4. 核对现有 18288 RDMS 应用登记和回调 `http://10.242.180.54:18288/kb/sso/callback`、client ID/Secret、验证地址、`RAG_WANSHITONG_SSO_ENTRIES`、`RAG_TRUSTED_ORIGINS`、可信代理、Cookie/deployment ID。候选当前只登记 8289，不能直接换端口。旧生产凭据不得写入仓库或日志；曾在聊天中暴露的管理令牌应在维护窗口轮换并处置旧会话。
5. 在隔离端口预置与旧生产等价的公共根别名，使 `/`、`/kb/`、`/kb/admin/`、静态资源、API、SSO 回调和原件下载路径都指向同一候选栈。保持 18288 原链路不动，核对 54/60 上的端口和 PID；不把新引擎连接到旧生产数据库。

## 本次切流步骤（历史记录，已执行）

1. 留存 54:18288 当前转发进程参数、旧生产根别名代理参数、回退镜像/配置及旧数据位置。先让候选栈以 18288 的合法身份配置在隔离链路自检；需要正式 origin 的 SSO 验证留到切流后。可选择提升当前 8289 栈并冻结测试写入，或恢复一致快照到新的隔离栈；两种方式都必须先明确 8289 与 18288 的数据归属。
2. 仅在受控窗口把 54:18288 转发目标切到准备好的新代理，保留原 60:8288/18290 服务和其卷不动。先核对 `/` 与 `/kb/` 的 HTTP 状态及资源路径，再用真实 RDMS 用户从正式域名登录，验证 Cookie 大小、回调、用户信息、问答流式正文、引用/原件、历史、反馈与管理设置读取。不能用 8289 的登录结果代替这一项。
3. 记录切流时间、实际镜像与配置摘要、真实烟测结果。只有全部通过并确认共享资源正常时开放首批邀请；无需在前端新增文字说明。

以上步骤保留切流前的准备脉络；实际结果和未完成的共享容量、快照恢复验证见[发布门禁](PILOT-RELEASE-GATE.md)。当前回退操作以本文件前面的“运行检查与故障处理”为准。回退不会把新栈试用会话或上传反写到旧生产，双方数据必须分别保留。
