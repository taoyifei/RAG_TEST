# API 鉴权与安全边界

P09 使用两个独立 Bearer Token。Query Token 只能执行 Search/Answer；Admin Token
用于生命周期写入、Artifact、Job、系统状态、显式 Provider Probe 和 Debug。
资源不存在与 scope 不匹配统一返回不泄露跨租户信息的 `NOT_FOUND`。

服务端不接受本地路径。上传先流式写入数据根内权限受限的临时文件，同时统计字节数；
超过上限立即返回 413，成功或失败都清理临时文件。客户端文件名只保留最终 basename，
不能控制物理位置。

CORS 默认不开放。完整 Retrieval Diagnostics 还受 `debug_enabled` 环境开关保护，
公开查询响应不含完整候选、向量、Prompt、Secret、Provider 原始响应或绝对路径。
Admin Trace 查询只返回既有安全事件和可公开 Job 快照；按 Job 查询也不暴露 fencing
token。

普通 readiness 仅检查本地状态，不因“配置了 Key”就报告 Provider healthy。
远程探测必须显式授权网络和请求预算；在 Jina/Qwen Live Calibration 未通过前，状态固定
报告 `remote_dense_confidence_calibrated=false` 和
`remote_production_profile_ready=false`。
