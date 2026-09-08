# V3-00.5 BuildKit 与发布安全证据修复

日期：2026-09-08

本阶段工程实现和开发门禁已完成，发布安全门仍按设计保持阻断。最终候选
镜像已由受支持的 BuildKit 白名单上下文重新构建并完成全量 Trivy 扫描；
50 个 High/Critical 包版本元组均无本发行版可用 `FixedVersion`，且没有人工
风险接受，因此没有运行正式 release acceptance，也没有发布、推送或合并。

## 状态

| 状态项 | 结果 | 依据 |
| --- | --- | --- |
| `ENGINEERING_DONE` | `PASS` | 构建上下文、逐项扫描证据和同一扫描复核入口均已实现并回归 |
| `DEV_STAGE_GATE` | `PASS` | 最终 `scripts/dev.py check` 为 2575 passed、89 deselected；smoke/Product/V3-00 基线均通过 |
| `BUILD_CONTEXT_GATE` | `PASS` | 默认 BuildKit 从仓库外不可变上下文成功构建，manifest、镜像 revision 和 root-only 元数据均已核对 |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | 50 个 High/Critical 元组仍为 `UNDER_INVESTIGATION`，0 个批准处置；freshness policy 仍为 `PROPOSED` |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | `verify` 阻断后按依赖失败即停 |
| `LIVE_QUALITY_EVIDENCE` | `NOT_RUN` | 未调用真实 OCR、远程模型或生产服务 |
| `NEXT_STAGE_ENTRY` | `PASS` | 只授权继续后续源码开发，不代表发布许可 |
| `MERGE_READINESS` | `BLOCKED` | 发布风险尚无人工作出接受决定；本阶段也未获 push/merge 授权 |

既有 `P11_READY` 和 `SECURITY_READY` 没有被新状态覆盖，仍为 `BLOCKED`。

## 范围和候选身份

- 起点包含 V3-00 提交 `53358081cb450f56fa98ffb899df334828a3e064`。
- 最终候选源码提交为
  `746ae66adf68e92b3180e4915c544e2d258d6df6`；正式构建前工作树干净。
- 后续 `fbdb6af283fc9bbe20a4b9ae7f043934d6a32e1a` 只刷新与最终扫描绑定的
  本地风险草案，不改变镜像运行内容。
- 未修改 Parser、Chunker、检索、生成链或索引语义；没有自动重建索引。
- `main`、`Industry` 和 `feature/universal-rag` 未修改；没有 push、merge、
  force-push、reset、stash、clean、rebase 或生产卷操作。

## 原始 BuildKit 失败点

当前环境为 Docker Engine/Client `29.4.0`、API `1.54`、Buildx
`v0.33.0-desktop.1`、默认 docker driver BuildKit `v0.29.0`、Compose
`v5.1.2`，WSL Git `2.34.1`，Python `3.11.15`。

原命令以仓库根 `.` 为 context。复现时 BuildKit 在 Dockerfile 的任何
`COPY/RUN` 之前停止：

```text
#11 [internal] load build context
#11 transferring context: 124B 0.9s done
#11 ERROR: error from sender: open artifacts/p11-final/trial-hardening/
app-update-20260907T021553Z/backups/trust-state/backup-trust: permission denied
```

因此失败进程是 BuildKit local context sender，不是 Git、打包器，也不是
容器内命令。`Dockerfile.dockerignore` 存在并优先于 `.dockerignore`；两者
都排除 `artifacts`，但 ignore 匹配不构成 sender 一定不会打开父路径的证明。
`.gitignore` 不参与 Docker context 规则。此前 legacy sender 成功只能证明
替代 sender 路径可用，不能证明默认 BuildKit 已修复。

本轮只用 `stat` 复核原路径顶层元数据，没有读取目录正文，也没有使用
`sudo`、`chmod` 或 `chown`：

```text
drwxr-xr-x 755 root:root .../backups/trust-state
drwx------ 700 root:root .../backups/trust-state/backup-trust
```

## 不可变发布 context

正式 `scripts/release.py build` 现在调用 `scripts/release_context.py`：

1. 从干净 HEAD 的 Git blob 对象按 `-z` 路径记录读取实际 Dockerfile
   所需白名单，不递归扫描或复制工作树。
2. 白名单覆盖 Dockerfile、Dockerfile-specific ignore、`pyproject.toml`、
   runtime lock、frontend source/lock、OpenAPI、`src`、migrations、
   compatibility manifest，以及 Dockerfile 实际引用的有限 evaluation 输入。
3. tracked 不等于安全：导出器再次拒绝秘密/私有资料/数据库/不允许的二进制，
   拒绝 symlink 和未知 Git mode，缺少必需输入即失败；`100755` 执行位按
   Git mode 保留。
4. context 建在仓库外的独立临时目录，不会把自身纳入扫描；构建结束后清理。
5. manifest 记录 source SHA、每个路径的 Git object ID、字节数、SHA-256
   和 Git mode，不写入密钥正文。

真实合成集成测试以非特权 `jerry` 身份创建 `000` 目录、敏感哨兵、中文和
空格路径、正常源码及越界链接；默认 BuildKit 成功构建，受限目录元数据未变，
镜像导出中不存在哨兵，缺输入和越界链接均安全失败。最终重跑结果为
`1 passed`。长期入口没有使用 `DOCKER_BUILDKIT=0`，也没有移除 Dockerfile
syntax digest。

最终 manifest：

- 路径：`artifacts/release-context/20260908T033643088890Z/context-manifest.json`
- SHA-256：`23f93787b85256626ab4a4b574adba4dbccb656658f426c89120749973ba7c6f`
- source revision：`746ae66adf68e92b3180e4915c544e2d258d6df6`
- 文件数：400
- 总字节：4,679,800
- effective ignore：`Dockerfile.dockerignore`

最终构建的 `[internal] load build context` 只传输 `4.71MB`，随后 Compose
config 校验通过。quickstart/deployment 文档已统一到正式 release build；
Compose 不再隐式从整个工作树构建。

## 最终镜像与扫描证据

| 字段 | 值 |
| --- | --- |
| 镜像 tag | `docx-rag:v1-candidate` |
| Docker image ID / 本地 RepoDigest | `sha256:b3dadcfd6fa91f2ad23cedcfbfa5e3f89a8ae7a27bcc4ca019ce62f8c00743a0` |
| registry manifest digest | `null`；本轮没有 push，不能把本地 image ID 冒充远端 manifest |
| 平台 | `linux/amd64` |
| OCI revision | `746ae66adf68e92b3180e4915c544e2d258d6df6` |
| 基础镜像 | `python:3.11-slim-trixie@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534` |
| Trivy scanner | `0.74.0`；镜像 digest `sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969` |
| Trivy ArtifactID | `sha256:ff81a14396ec1b60a7232977afc60b92429e15375c33b215ed0cd5ea43835656` |
| 扫描时间 | `2026-09-08T03:40:25.328043384Z` |
| DB UpdatedAt / DownloadedAt | `2026-09-08T01:14:11.072075664Z` / `2026-09-08T03:40:22.048223399Z` |

Trivy `ArtifactID` 是 scanner 的 artifact 身份，实测与 Docker
`Metadata.ImageID` 不相等。复核入口不再错误要求二者相等；仍严格绑定
`Metadata.ImageID`、本地 RepoDigests、平台、原始 JSON SHA、扫描时间、
scanner/DB 身份、OCI revision 和基础镜像。

受控证据：

- 完整扫描：`artifacts/v3-00-5-final/security/20260908T033901000258Z/trivy-all.json`，
  SHA-256 `7458d9114dd5681978d91cbb115d2647d4eb5cd67c2158c6e4fc2a91a571f05f`。
- DB metadata：同目录 `trivy-db-metadata.json`，SHA-256
  `dbc459edda2f9e0c98492d996dbc25f318298539b8bb0d83e96d0b7ae025ea16`。
- 可修复子门：同目录 `trivy-fixable.json`，SHA-256
  `f4cf2041cba650cf629ba744fe3996b0cda6b7568d5ef0805768cb8dceb61eb6`。
- SBOM：同目录 `sbom-image.cdx.json`，SHA-256
  `da1d5c176a683272ae490b1790a972915e874eef1f9e57fc9f0739cac273abbd`。
- License inventory：同目录 `licenses-image.json`，SHA-256
  `0b7cf6228870d7b1d3623de405d6fac49341036d672eb9a9e55fdfc92d520818`。
- 逐项调查草案：`release/p11-os-risk-review.json`，SHA-256
  `3c28fb85dc1061152a4f57e66271371f0c4434ec21b033682097e1a8b1e85c7a`。
- 同一扫描复核：`artifacts/v3-00-5-final/os-risk-review-recheck.json`，
  SHA-256 `7b929a3d842d915cdd6c9f6f948a9a60d010a237763cc0813c451eaec875f0d1`。
- 聚合 evidence：`artifacts/v3-00-5-final/evidence.json`，SHA-256
  `26c29dc30de5da79b9e92bb52a2d7b47077627dd9b576e3149a160df44380fbe`。

完整扫描含 173 条原始发现；High/Critical 为 50 个 CVE+包+installed
version 元组、18 个唯一 CVE，`FixedVersion` 非空为 0，47 个状态为
`affected`、3 个为 `fix_deferred`。与上一候选扫描相比，50 元组集合新增 0、
移除 0。`--ignore-unfixed` 子门仍保留且可修复 High/Critical 为 0，但没有
用它替代完整扫描和 OS 风险门。

## 18 个 CVE 与 50 个包版本元组

下表按唯一 CVE 聚合；[逐项草案](../../release/p11-os-risk-review.json) 保留
全部 50 行的 package、installed/source version、PURL、arch、Target、layer、
原始 result/vulnerability/package 索引与 fingerprint，以及触发条件、上传/网络
可达性、缓解、剩余影响和建议动作。

`U8` 表示同一 `util-linux=2.41.5-0+deb13u1` 源版本的八个扫描元组：
`bsdutils=1:2.41.5-0+deb13u1`、`libblkid1=2.41.5-0+deb13u1`、
`liblastlog2-2=2.41.5-0+deb13u1`、`libmount1=2.41.5-0+deb13u1`、
`libsmartcols1=2.41.5-0+deb13u1`、`libuuid1=2.41.5-0+deb13u1`、
`login=1:4.16.0-2+really2.41.5-0+deb13u1` 和
`util-linux=2.41.5-0+deb13u1`。

| CVE | 元组 | source / installed package | Trivy | 调查边界 |
| --- | ---: | --- | --- | --- |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | 4 | `ncurses=6.5+20250216-2`；`libncursesw6/libtinfo6/ncurses-base/ncurses-bin` 同版本 | affected / 无 FixedVersion | `infocmp` 本地工具条件；上传不产生终端描述，但本地恶意输入剩余风险未批准 |
| [CVE-2026-11822](https://security-tracker.debian.org/tracker/CVE-2026-11822) | 1 | `sqlite3=3.46.1-7+deb13u1`；`libsqlite3-0` 同版本 | affected / 无 FixedVersion | 应用使用组件；需要恶意数据库条件，不能据此判定无风险 |
| [CVE-2026-11824](https://security-tracker.debian.org/tracker/CVE-2026-11824) | 1 | `sqlite3=3.46.1-7+deb13u1`；`libsqlite3-0` 同版本 | affected / 无 FixedVersion | 与上项相同，保留数据库输入和备份恢复边界 |
| [CVE-2026-13221](https://security-tracker.debian.org/tracker/CVE-2026-13221) | 1 | `perl=5.40.1-6`；`perl-base` 同版本 | affected / 无 FixedVersion | 本地解释器条件；服务不接收 Perl 代码，但不能统一免疫 |
| [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | 2 | `systemd=257.13-1~deb13u1`；`libsystemd0/libudev1` | affected / 无 FixedVersion | 所需 daemon 不在运行镜像中；共享库仍存在 |
| [CVE-2026-41992](https://security-tracker.debian.org/tracker/CVE-2026-41992) | 1 | `gzip=1.13-1` | affected / 无 FixedVersion | 本地工具条件；不把非 root 作为无风险证明 |
| [CVE-2026-42496](https://security-tracker.debian.org/tracker/CVE-2026-42496) | 1 | `perl=5.40.1-6`；`perl-base` | fix_deferred / 无 FixedVersion | 所需模块缺失，仍待人工确认剩余风险 |
| [CVE-2026-42497](https://security-tracker.debian.org/tracker/CVE-2026-42497) | 1 | `perl=5.40.1-6`；`perl-base` | fix_deferred / 无 FixedVersion | 所需模块缺失，仍待人工确认剩余风险 |
| [CVE-2026-48962](https://security-tracker.debian.org/tracker/CVE-2026-48962) | 1 | `perl=5.40.1-6`；`perl-base` | affected / 无 FixedVersion | 所需模块缺失，不等于发行版已修复 |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | 1 | `acl=2.3.2-2`；`libacl1=2.3.2-2+b1` | affected / 无 FixedVersion | 特权本地调用者条件；服务非 root 只是缓解 |
| [CVE-2026-57432](https://security-tracker.debian.org/tracker/CVE-2026-57432) | 1 | `perl=5.40.1-6`；`perl-base` | affected / 无 FixedVersion | 本地解释器条件 |
| [CVE-2026-57433](https://security-tracker.debian.org/tracker/CVE-2026-57433) | 1 | `perl=5.40.1-6`；`perl-base` | affected / 无 FixedVersion | 所需模块缺失，仍保持阻断 |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | 8 | `U8` | affected / 无 FixedVersion | 已移除 `mount` 命令但保留必需 libmount/prlimit；特权自定义调用仍是剩余风险 |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | 8 | `U8` | affected / 无 FixedVersion | 特权 `nsenter` 本地操作条件；应用不调用，但未批准风险接受 |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | 8 | `U8` | affected / 无 FixedVersion | 当前 kernel 前置不满足；不能外推到所有部署宿主 |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | 8 | `U8` | affected / 无 FixedVersion | `mount` 命令已移除但共享组件仍存在 |
| [CVE-2026-8376](https://security-tracker.debian.org/tracker/CVE-2026-8376) | 1 | `perl=5.40.1-6`；`perl-base` | affected / 无 FixedVersion | 当前架构前置不满足，仍不等于全平台免疫 |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | 1 | `perl=5.40.1-6`；`perl-base` | fix_deferred / 无 FixedVersion | 所需模块缺失，等待同发行版修复和人工决定 |

2026-09-08 逐页复核 Debian Security Tracker 时，上述已安装 trixie 源版本仍
列为 vulnerable。部分页面列出 unstable/forky/sid 的更高版本，不是当前
trixie 兼容系列的可用修复，因而未为降低数字切换发行版、删除 antiword、
prlimit、字体或共享运行库。`no-dsa`、postponed、组件未加载、内网、非 root、
cap drop 或 scan exit 0 都没有被解释为统一免疫或风险批准。

## 同一扫描人工审核接续

新增 `release.py review-os-scan` 会复核已有不可变 `trivy-all.json`，不运行
Trivy、不替换扫描时间/hash，也不把 S2 冒充 S1。它调用与 `verify` 相同的
风险聚合逻辑，并重新验证当前候选身份、完整未过滤扫描、DB metadata、处置
覆盖、scope/有效期、freshness policy 和其他发布证据身份。

最终扫描相对上一扫描只有镜像/扫描身份变化，50 元组完全匹配。生成器只移植
50 项技术调查文字并显式记录：

```text
technical_assessment_reuse.status=PORTED_UNDER_INVESTIGATION
matched_tuple_count=50
approval_state_transferred=false
requires_human_revalidation=true
```

新草案 50/50 为 `UNDER_INVESTIGATION`，owner、approver、expires_at 均为
`null`，`risk_accepted=false`，没有可通过校验的批准附件。最终同一扫描复核
确认候选身份匹配且 `review_errors=[]`，但 freshness policy 仍为
`REVIEW_FRESHNESS_POLICY_NOT_APPROVED`，命令退出 1：

```text
BLOCKED release: SECURITY_READY=BLOCKED: RISK_REVIEW_REQUIRED
OS_RISK_REVIEW status=BLOCKED reason=RISK_REVIEW_REQUIRED
```

合成测试覆盖同一 scan/镜像的有效人工 fixture 可以通过，以及变 hash、变镜像、
过期、漏一个包版本元组、未知可达性、纯自由文本和可修复项试图豁免均不能通过。
fixture 明确为测试数据，没有进入真实 release 证据。模型没有创建批准或冒用签名。

## 开发门禁中的时钟回拨修复

最终全量门禁第一次复验时，WSL 墙钟在新 Provider 验证落库后回拨
`0.49–0.52s`；连接版本、凭据版本、端点、模式和状态全部匹配，但 Python
判定和 SQLite 发布事务都严格要求 `finished_at <= now`，造成不同 Product
fixture 间歇性 409。

两道校验现统一允许最大 5 秒的未来时钟偏差，24 小时 TTL 不变；回归明确验证
`+1s` 接受、`+10s` 拒绝。该变更属于运行时代码，因此没有复用旧候选，而是
提交后重新构建并重扫最终镜像。

## 实际验证

| 命令 | 退出状态 | 结果 |
| --- | ---: | --- |
| `.venv/bin/python scripts/dev.py check` | 0 | compileall、Ruff、359 个 mypy 源文件、docstring 通过；2575 passed、89 deselected、4 warnings |
| pytest marker collect-only | 0 | 总计 2664；79 个 `local_integration`、10 个 `live_provider`，二者无重叠；默认离线门禁排除 89 个 |
| `.venv/bin/python scripts/dev.py smoke` | 0 | 72 passed、1 个既有弃用 warning |
| `.venv/bin/python scripts/dev.py product-check` | 0 | hardcode audit 通过；74 passed、1 warning |
| `.venv/bin/python scripts/dev.py product-smoke` | 0 | 6 passed、1 warning |
| V3-00 Word/streaming 两个 baseline 文件 | 0 | 5 passed、1 warning；保护旧阶段基线，没有改缺陷特征 |
| `pytest -q tests/integration/test_release_context_buildkit.py` | 0 | 1 passed；真实默认 BuildKit、非特权受限目录和镜像哨兵检查 |
| 风险草案/同一扫描审核测试 | 0 | 41 passed；Ruff 通过 |
| `.venv/bin/python scripts/release.py build` | 0 | 4.71MB 白名单 context；候选与 Compose config 通过 |
| `.venv/bin/python scripts/release.py verify --evidence-file artifacts/v3-00-5-final/evidence.json` | 1 / `BLOCKED` | pip-audit 0、npm audit 0、runtime 无 node/pip/wheel、secret scan 1404 files、SBOM/license 完成；OS 风险门阻断 |
| `release.py review-os-scan ...` | 1 / `BLOCKED` | 不重扫；候选身份通过，50 项仍待调查/人工决定，freshness policy 未批准 |
| `.venv/bin/python scripts/release.py acceptance` | `NOT_RUN` | `verify` 阻断后未执行 |
| 真实 OCR、远程模型、生产发布 | `NOT_RUN` | 无相应授权；离线/Mock 证据不冒充 Live |

完整开发门禁的 4 个 warning 中，1 个是既有 Starlette/httpx 弃用提示，另
3 个来自合成 Qdrant 的不安全连接/无法取得测试 server version；它们不属于
`release verify` 输出，也没有被隐藏或改成通过断言。89 个 deselected 是明确
标记的本地集成/真实 Provider 边界；本阶段必需的真实 BuildKit 集成已另外执行，
真实 Provider 不属于 V3-00.5。

## 提交与下一步

本报告落盘前的本阶段本地提交如下；报告提交本身以最终 Git 历史为准：

- `2a4347978ff850b0810f94d99e76e8fd7c97f343`
  `fix(release): use an immutable BuildKit context`
- `8aa7b0c4b76c25ee0076827dbb19ae122308d603`
  `fix(product): tolerate bounded provider clock skew`
- `746ae66adf68e92b3180e4915c544e2d258d6df6`
  `fix(release): validate immutable scan image identity`
- `fbdb6af283fc9bbe20a4b9ae7f043934d6a32e1a`
  `docs(v3-00.5): refresh final OS risk draft`

人工发布管理员仍需逐项确认 50 个处置、填写 owner/approver/有效期并批准
freshness policy；任何 scan/image/hash/DB 身份变化都必须重新核对。完成前
`RELEASE_SECURITY_GATE`、`RELEASE_ACCEPTANCE` 和 `MERGE_READINESS` 保持当前
状态。V3-00.5 到此停止，不提前进入 Parser/Chunker/生成链实现。
