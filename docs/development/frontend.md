# 前端开发与验证

## 环境

需要 Node 24、npm 11、Python 3.11 与仓库虚拟环境。依赖锁定在
`frontend/package-lock.json`；首次安装执行：

```bash
npm --prefix frontend ci --ignore-scripts
python scripts/dev.py web-install-check
```

若找不到 Linux Node/npm，统一入口返回 `BLOCKED`，不会把缺环境写成 PASS。

## 开发与生产入口

开发模式可分别启动 Vite 和离线 FastAPI；Vite 只代理 loopback `/api`、`/live`、
`/ready`。生产模式先构建一次静态 assets，再由 FastAPI 提供唯一 SPA 入口：

```bash
npm --prefix frontend run build
.venv/bin/python scripts/serve_p10.py --profile configs/profiles/dev-offline.json
```

开发 Token 不写入源码或 Vite 配置。

## 统一门禁

```bash
python scripts/dev.py web-lint
python scripts/dev.py web-typecheck
python scripts/dev.py web-test
python scripts/dev.py web-build
python scripts/dev.py web-e2e --profile configs/profiles/dev-offline.json
```

`web-build` 先用冻结的 `docs/public/openapi-v1.json` 检查生成类型是否最新。E2E 启动
临时数据目录和 loopback-only 服务，默认不带任何远端 Provider 环境变量。在 WSL 没有
Playwright Chromium、但宿主 Windows 已安装 Chrome 时，统一入口可由 Windows Node
驱动该浏览器访问同一 WSL loopback 服务；测试语义不变。

不提交 `node_modules`、`dist`、coverage、Playwright report、test-results、截图、视频或
Trace。`src/api/schema.d.ts` 是生成物，不由 Prettier 改写。

