# 工业智能体

工业智能体面向设备手册、操作规程、故障案例和业务制度等资料，提供自然语言检索、问答、来源引用与原件下载。实际覆盖的业务取决于知识库中的文档。

## 功能

- 上传资料后提取文本、表格与图片，按结构切分，建立全文与向量索引。
- 在用户有权访问的资料范围内检索，融合关键词和语义结果，对候选片段重排并生成回答。可限制在指定文件中检索。
- 支持查看原文和图片、下载附件、查询模板和操作指引，以及管理历史会话。
- 普通用户可查看公共资料和维护本人资料。管理员维护公共资料，人力角色与超级管理员维护人力内部资料。公开人力制度可由管理员、人力角色和超级管理员维护。
- 个人资料默认上限为 250 MB、50 份，单文件最多 50 MB。资料上传和版本更新按当前用户权限执行。

## 代码入口

| 目录 | 内容 |
| --- | --- |
| [src/industrial_agent](src/industrial_agent/) | 身份接入、角色与资料范围、会话和问答网关 |
| [frontend](frontend/) | 用户问答、资料和会话界面 |
| [vendor/knowledge-engine](vendor/knowledge-engine/) | 检索、重排、生成、解析及管理端源码 |
| [deployment/industrial-agent](deployment/industrial-agent/) | 独立部署、参数示例和初始化脚本 |
| [tests](tests/) | 登录、权限、资料管理和问答回归测试 |

更多实现细节见[知识引擎说明](docs/knowledge-engine.md)。构建、登录和模型配置见[部署说明](deployment/industrial-agent/README.md)。本分支用于本地学习，运行实例时需要配置身份服务、模型端点和业务资料。

## 本地检查

```sh
python -m pip install -r requirements.lock
python -m pytest tests/industrial_agent tests/identity
python -m ruff check src/industrial_agent
cd frontend
npm ci
npm test
npm run build
```

Python 使用 3.11，前端使用 Node.js 24。

## 许可与依赖

引擎来源版本、原始提交及许可文件摘要见 [upstream.lock.json](upstream.lock.json)。著作权署名与许可正文见 [LICENSE](vendor/knowledge-engine/LICENSE)，依赖组件说明见 [THIRD_PARTY_NOTICES.md](vendor/knowledge-engine/THIRD_PARTY_NOTICES.md)。
