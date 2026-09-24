# WeKnora 分块器固定快照

`internal/infrastructure/chunker/*.go` 从
`Tencent/WeKnora@1edcd54b43606d9079bb36650efe3f68707a79ea`（v0.8.0）
逐字节复制，包含上游测试；同目录附带上游 `LICENSE`。

两个小型依赖桥接保留原导入路径，避免带入 WeKnora 应用的无关依赖：

- `internal/logger/logger.go` 将两处诊断日志转给标准库 `slog`。
- `internal/infrastructure/docparser/image_unwrap.go` 复制上游
  `image_resolver.go` 中的 `UnwrapLinkedImages` 实现。

分块算法文件未修改。`cmd/wb-chunker` 是本方 JSON 标准输入输出适配命令，
不属于上游代码。使用 Go 1.26.0 构建：

```sh
./scripts/build_weknora_chunker.sh
```

仓库内 Linux amd64 二进制固定 SHA-256 为
`491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`。
应用只在创建候选索引时加载该二进制；默认仍使用旧分块器。
