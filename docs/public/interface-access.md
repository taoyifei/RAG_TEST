# 接口访问

“接口访问”页面用于向外部 SDK 创建作用域 Bearer Token。选择最小必要 scope，并尽量
绑定项目、知识库和过期时间。

完整 Token 只显示一次。复制后妥善保存；关闭提示后服务无法恢复原值。如 Token 泄露或
不再使用，应立即吊销。列表只显示名称、scope、绑定范围、创建/使用/过期/吊销时间。

```http
Authorization: Bearer ragk_<one-time-value>
```

`query:read` 不能读取系统状态，知识库级 Token 也不能越过绑定的 project 和 knowledge
base。管理员浏览器不使用这些 Token，而使用 HttpOnly Session Cookie 和 CSRF。
