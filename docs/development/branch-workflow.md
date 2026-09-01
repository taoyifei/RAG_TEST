# 通用 RAG 分支工作流

## 固定分支

- `main`：正式主分支，本路线只读。
- `Industry`：旧实验与工业部署参考，本路线只读且禁止整体合并。
- `feature/universal-rag`：阶段集成分支。
- `codex/pNN-*`：从最新集成分支创建的单阶段开发分支。

每个阶段只允许 `stage -> feature/universal-rag` 的 `--no-ff` 合并。禁止把阶段分支
直接合入 `main`/`Industry`，禁止 force push、共享历史改写和未经说明的 rebase。

## 开始阶段

PowerShell、Linux 和 macOS 的 Git 子命令相同：

```text
git remote -v
git fetch origin --prune
git status --short
git branch --show-current
git log -1 --oneline origin/feature/universal-rag
git switch feature/universal-rag
git pull --ff-only origin feature/universal-rag
git switch -c codex/pNN-topic
git push -u origin codex/pNN-topic
```

若同名分支已存在，先检查 merge-base、远程额外提交和已合并状态，不删除或覆盖。
工作区不干净时记录未知修改并停门，不 reset、stash、clean 或丢弃用户工作。

## 开发与验证

```text
python scripts/dev.py doctor
python scripts/dev.py check
python scripts/dev.py smoke
git diff --check
git status --short
```

提交按单一意图拆分并遵循 Conventional Commits。只 stage 本阶段文件，提交前检查
`git diff --cached --stat` 和 `git diff --cached --check`。

## 阶段合并

```text
git push origin codex/pNN-topic
git switch feature/universal-rag
git pull --ff-only origin feature/universal-rag
git merge --no-ff codex/pNN-topic
python scripts/dev.py doctor
python scripts/dev.py check
python scripts/dev.py smoke
git push origin feature/universal-rag
```

推送后用 `git ls-remote` 核对远程阶段分支和集成分支 SHA。保留远程阶段分支供审计，
不自动删除。
