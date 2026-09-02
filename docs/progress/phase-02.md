# 阶段 2 进度：Jina 主用与 Qwen3.7 Embedding 热备

## 状态

- Integration base：`464f0a731648af048c9c677cfbf0ee2c27270d4a`。
- Feature branch：`codex/p02-external-providers`。
- 实现提交：`38ca7ab`；测试提交：`9efdae2`；Provider 文档提交：`9bb6e6d`。
- Integration implementation merge commit：
  `b4c0b06ec311ad148c8acfa40c2ff68716e6e77d`（`--no-ff`）。
- 远程状态：`origin/codex/p02-external-providers@9bb6e6d` 和
  `origin/feature/universal-rag@b4c0b06` 已由 `git ls-remote` 核对。
- `main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 与
  `Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` 保持只读。

## 实际修改文件

- Provider 与兼容层：`src/rag_app/adapters/providers/` 下的 `__init__.py`、
  `http_common.py`、`batching.py`、`validation.py`、`deterministic.py`、`jina.py`、
  `aliyun_qwen37.py`、`legacy.py`，以及
  `src/rag_app/adapters/legacy/providers.py`。
- 应用层：`src/rag_app/application/embedding_router.py`、
  `provider_health.py` 和 `__init__.py`。
- Core 与装配：`src/rag_app/core/errors.py`、`core/__init__.py`、
  `core/models/provider.py`、`core/models/__init__.py`，以及
  `src/rag_app/composition/builtin_providers.py`、`provider_profiles.py`、
  `profiles.py`、`registry.py`、`factory.py`、`composition/__init__.py`。
- 配置与入口：`configs/profiles/catalog.json`、`dev-offline.json`、
  `dev-jina-only.json`、`dev-jina-qwen37-hot-standby.json` 和 `scripts/dev.py`。
- 自动测试：`tests/adapters/providers/` 下三个合同模块、
  `tests/application/test_embedding_router.py`、`test_provider_health.py`、
  `tests/composition/test_provider_profiles.py`、`tests/test_provider_cli.py`。
- 文档：ADR 0004、`docs/design/provider-contracts.md`、三个 `docs/public/`
  Provider 文档和本报告。实现合并共修改 39 个文件，新增 5,593 行、删除 75 行。

## 实现与迁移

- 新增固定 Jina v5 small Embedding、Jina v3.5 Reranker 和北京地域原生
  Qwen3.7 Embedding adapter；所有远程配置从环境变量延迟读取，构造时不发网络。
- 新增同步长生命周期 HTTP client、有界重试、`Retry-After`、响应上限、严格 JSON/索引/
  维度/有限值校验和脱敏 `ProviderCall`。
- 新增双槽 Router、进程内并发安全 circuit、阿里 UTC 日预算、目的地级出网门、请求内
  slot 粘性、缓存键隔离、Reranker 显式旁路和双 Provider 文档协调合同。
- 默认离线 Profile 仍使用 deterministic/lexical 实现；远程 Profile 必须显式选择并授权。
  P02 没有创建、重建或激活 Qdrant collection，也没有迁移 SQLite 或用户数据。

## Schema 与公共接口兼容

Python 公共 schema 只做兼容性扩展：`ProviderCall` 新字段均为可选，新增
`ProviderFailureCategory` 与 `DenseUnavailable`，Profile 增加可选 Provider 身份字段和
显式安全配置。现有 HTTP/SDK schema、SQLite schema、Qdrant schema、旧索引和旧 Provider
调用路径未破坏；Legacy adapters 为单向兼容桥。改变 Provider、模型、角色、instruction、
维度或 normalization 会改变指纹并要求后续阶段创建新 revision。

## 验证记录

起始集成分支：

```text
.venv/bin/python scripts/dev.py check
compileall/ruff/mypy/Google docstrings: passed
998 passed, 75 deselected, 4 warnings in 178.50s
```

开发期门禁曾真实失败一次：新增 Registry 协议的两个方法共缺 4 个 Google docstring
小节，`scripts/dev.py check` 以退出码 1 停在 docstring 门禁，pytest 未运行。补齐四个小节后
从完整命令重跑；没有删除或跳过失败用例。早期合同测试还发现 5 个 Mock fixture 构造错误，
修正 fixture 后再执行最终验收。

提交前最终结果：

```text
.venv/bin/python -m pytest -q tests/adapters/providers tests/application/test_embedding_router.py tests/composition
84 passed in 0.35s

.venv/bin/python scripts/dev.py provider-check --profile configs/profiles/dev-jina-qwen37-hot-standby.json
profile/fingerprints: OK; network_calls=0

.venv/bin/python scripts/dev.py check
compileall: passed
ruff: All checks passed
mypy: Success, 165 source files
Google docstrings: missing_google_sections=0
1070 passed, 75 deselected, 4 warnings in 180.30s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 1.56s

.venv/bin/python scripts/dev.py failover-smoke --scenario <scenario>
jina-timeout/jina-429/jina-bad-dimension: standby/dense_standby
both-unavailable: DENSE_UNAVAILABLE
```

合并后的 `feature/universal-rag@b4c0b06ec311ad148c8acfa40c2ff68716e6e77d`：

```text
.venv/bin/python -m pytest -q tests/adapters/providers tests/application/test_embedding_router.py tests/composition
84 passed in 0.36s

.venv/bin/python scripts/dev.py provider-check --profile configs/profiles/dev-jina-qwen37-hot-standby.json
profile/fingerprints: OK; network_calls=0

.venv/bin/python scripts/dev.py check
compileall/ruff/mypy/Google docstrings: passed
1070 passed, 75 deselected, 4 warnings in 181.61s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 1.52s
```

最终门禁失败数为 0。`check` 明确跳过 75 个 `local_integration` 或 `live_provider` 测试，
没有把它们写成通过。

## 外部服务与数据边界

External services actually called（应用数据面）：none。

- Jina live contract：not executed。
- Aliyun qwen3.7 live contract：not executed。
- Automatic failover：verified with injected transports only。
- 交付控制面访问了 GitHub origin；API 字段核对只读取 Jina 与阿里官方公开文档。
- 没有读取真实 API Key，没有上传企业文档，没有调用 LLM、OCR 或 Qdrant 服务。测试只用
  合成短文本和注入 `MockTransport`。

## 决策与剩余风险

没有触发公共 Schema 破坏、真实企业文档出网、付费服务、primary-only 激活、未授权备用、
不可逆迁移或合入 `main` 的决策门；无待回答决策。

P02 只证明接口、状态机、数据隔离和故障路径。Jina/Aliyun 账号可用性、语义质量、真实成本
和限流尚未验证；进程内 circuit 与预算不提供跨进程全局保证。双 named-vector 的真实
Qdrant 构建、抽样读回与原子激活属于后续索引阶段。现有 4 个警告为 Starlette/httpx 弃用
和测试构造中的 Qdrant HTTP/版本探测警告，不影响本阶段离线结果。
