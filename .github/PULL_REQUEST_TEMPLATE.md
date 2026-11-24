## 变更说明

<!-- 简述本次 PR 解决的问题或新增的能力，并关联对应 Issue（如 Closes #12） -->

## 变更类型

- [ ] Bug 修复（`fix`）
- [ ] 新功能（`feat`）
- [ ] 重构 / 性能优化（`refactor`）
- [ ] 文档 / 治理（`docs` / `chore`）

## 自检清单

- [ ] 本地单元测试全部通过（`uv run pytest -v`，且无真实外网请求）
- [ ] Ruff 静态检查通过（`uvx ruff check src tests`）
- [ ] 如修改 `config.py`，已同步更新 `config.example.yaml` 与 `docs/configuration.md`
- [ ] 确认未包含 `config.yaml`、`cookies/`、真实 Cookie/Token 或本机绝对路径
