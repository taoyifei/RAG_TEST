# Embedding 自动切换

## 不变量

自动切换是“选择另一个完整向量空间”，不是混合向量：

```text
Jina query vector  -> dense_primary -> Jina document vectors
Qwen query vector  -> dense_standby -> Qwen document vectors
```

禁止把 Qwen query 向量用于 `dense_primary`，也禁止在一次故障切换查询里合并、平均或比较
两个 Dense 排名。Router 返回的 `selected_slot_id` 与 `vector_name` 是请求内粘性结果，
分页、邻块扩展和缓存回填必须继续使用它们。

## 路由顺序

1. 验证 primary coverage、vector name 和维度；
2. 验证 Jina query 总授权和目的地授权；
3. circuit 允许时调用 Jina；
4. 成功则返回 `primary/dense_primary`，不调用备用；
5. 只有 transient、response contract、auth/model 失败允许继续；
6. 验证 standby coverage、阿里 query 授权、failover 开关、本地日预算和 circuit；
7. 调用 Qwen 并返回 `standby/dense_standby`；
8. 两者失败时返回 `DENSE_UNAVAILABLE`。

输入无效、策略拒绝和 Store 不兼容不会切换。这样可以避免备用掩盖调用方 bug、未授权
出网或错误索引。

## 离线故障 Smoke

下列命令全部使用注入 Provider，不访问公网：

```bash
.venv/bin/python scripts/dev.py failover-smoke --scenario jina-timeout
.venv/bin/python scripts/dev.py failover-smoke --scenario jina-429
.venv/bin/python scripts/dev.py failover-smoke --scenario jina-bad-dimension
.venv/bin/python scripts/dev.py failover-smoke --scenario both-unavailable
```

前三种应选择 `standby/dense_standby`；最后一种应输出 `DENSE_UNAVAILABLE`。

## Circuit 恢复

默认连续两次失败后打开 60 秒。冷却后由下一次真实请求获取唯一 HALF_OPEN lease；系统不
在后台消耗 Token。只有连续三次成功才恢复 CLOSED。合同错误被隔离，鉴权或模型错误在
readiness 中表现为配置降级。Embedding 和 Reranker 使用独立 circuit key。
