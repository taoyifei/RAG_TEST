# 把 RAG 作为 Python 组件嵌入宿主

P01 不要求启动 FastAPI。宿主创建可信 Registry、选择严格 Profile，并把 Composition
Root callback 显式交给同步 `RagEngine`：

```python
from rag_app.application.engine import RagEngine
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_offline_profile,
    register_builtin_components,
)

registry = ComponentRegistry()
register_builtin_components(registry)
profile = default_offline_profile()

with RagEngine.from_profile(
    profile,
    builder=lambda selected: build_components(selected, registry),
) as engine:
    for component in engine.component_info():
        print(component.kind, component.name, component.version)
```

可执行版本见 `examples/embedded_components.py`。默认 Profile 完全离线，Deterministic
Embedding 只能证明接口、隔离和状态机正确，不能证明语义质量。

宿主也可用 `build_components(..., overrides={"embedding_primary": instance})` 显式覆盖
单个实例。override 必须提供匹配的 descriptor/capability，失败时仍由组件集合统一关闭。
这不是全局 Service Locator，也不会根据用户输入动态加载模块。

固定 Jina/Qwen3.7 Profile 可用于检查模型、slot、named-vector 和指纹配置；P01 对真实
Provider 的 `embed/rerank` 调用会明确报不可用。P02 提供 HTTP adapter 后，调用前仍须
分别满足 Jina/阿里出网授权、secret 环境变量和费用预算。
