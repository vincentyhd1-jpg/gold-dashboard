# Repository workflow

1. 不直接在 `main` 上开发功能。
2. 每个阶段使用独立功能分支。
3. 开发前先 `fetch origin`，并确认分支基于最新 `origin/main`。
4. 禁止 force push `main`。
5. 禁止使用 `reset --hard` 处理未确认的用户工作。
6. 不得删除或覆盖无法确认来源的本地修改。

# Sources of truth

处理业务逻辑前必须阅读：

- `AGENTS.md`
- `CLAUDE.md`
- `docs/handover-technical.md`
- `docs/handover-decisions.md`

如果文档与实际代码冲突：

- 稳定的数据与验证原则优先保留。
- 状态性描述必须用代码和 Git 历史核实。
- 不得仅根据 `CLAUDE.md` 中过期的 TODO 修改代码。

# Data safety

1. CME 等不可再生原始数据不得因其他步骤失败而丢失。
2. 坏数据必须在采集层隔离。
3. 指标计算必须位于派生层，前端只消费派生字段。
4. 不允许伪造缺失数据：
   - 不随意 forward-fill。
   - 不以最近值替代不同频率数据。
5. schema 修改遵循 reader-first migration。
6. 数据写入必须保持项目已有的原子写入与幂等原则。

# Testing

1. Python 测试按项目规则使用 WSL Ubuntu-22.04。
2. 前端 `.mjs` 验证使用 Windows 原生 Node。
3. 新关键保护必须做反恒真 / 破坏注入，证明错误真的会让测试变红。
4. 不允许通过以下方式让测试通过：
   - 删除测试。
   - 放松断言。
   - 使用恒真断言。
5. 测试数量增加不等于覆盖增加。
6. 环境失败和产品代码失败必须明确区分。

# Pull request workflow

每个阶段：

1. 在功能分支工作。
2. 完成实现。
3. 跑定向测试。
4. 跑要求的项目级回归。
5. 自查 `git diff`。
6. commit 到功能分支。
7. push 功能分支。
8. 创建或更新 Draft PR。
9. PR body 必须包含：
   - Scope
   - Files changed
   - Design decisions
   - Tests
   - Negative/injection tests
   - Known risks
   - Out of scope
10. 不自行 merge。

# Review workflow

PR 创建后：

1. 使用 `gh` 检查当前 PR review 和 review threads。
2. 若存在 `REQUEST_CHANGES`：
   - 逐项分析。
   - 修复。
   - 重跑相关测试。
   - push 新 commit。
3. 不得为了满足 review 而破坏项目既定数据语义。
4. 有歧义或涉及产品决策时停止，等待用户。
5. review 全部处理后，在 PR 中留下简洁更新说明。
6. 等待下一轮 review。
7. 只有用户明确批准后才允许 merge。

# Scope discipline

1. 一个 PR 只解决一个阶段。
2. 不顺手处理无关技术债。
3. C4 不处理 README、ES modules、旧死代码等非 C4 项目。
4. 发现无关问题只记录，不扩大 scope。

# Credentials and networking

1. 不把 token、API key、密码写入代码、日志、报告或 commit。
2. 不读取或展示无关凭据。
3. 网络权限不足时明确报告，不通过绕过安全机制解决。

# Reports

1. 长报告优先写入本地 `codex_reports/`。
2. `codex_reports/` 不进入 Git。
3. PR 是 Codex 与外部 reviewer 的主要交接载体。
4. 不要求用户人工搬运长报告。
