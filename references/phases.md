# 各阶段详细做法

命令里标了「用前核对」的，是在写这份文档时没有实际运行过的命令。用之前按工具官方文档核对一遍，不凭这里的写法直接跑。

---

## 阶段 0 · 准备

```bash
cd <项目>
git status                              # 有未提交的改动先提交或暂存
git switch -c governance/cleanup        # 所有治理都在这个分支上做
git tag governance-baseline             # 随时能退回治理开始前的状态
```

- 按项目 README 或现有文档装依赖、启动项目。启动不起来就停在这里，先修环境，不进体检。
- 现有测试跑一遍，把"多少条、红几条、绿几条、用的命令"写进 PROGRESS.md。
- 把 `.governance/` 加进版本管理（基线要提交，CI 要用），但体检报告可以不提交：
  在 `.gitignore` 里加 `.governance/audit-*.md` 和 `.governance/metrics-*.json`。

## 阶段 1 · 体检

```bash
python3 ~/.agents/skills/project-governance/scripts/gov_audit.py audit --root . --save-baseline
```

1. 先看报告里有没有"失败"状态的指标。失败多半是环境问题：没装依赖、没有 lock 文件。修好再重跑，不要带着失败的指标存基线。
2. 生成代码、测试样本、第三方拷贝进来的代码不该算进指标，用 `--exclude` 排除后加 `--force` 重存基线。
   排除规则（连同内置的 node_modules、.next、dist 等）会传给每个自己扫目录的工具：ruff 用 `--extend-exclude`，
   pyright 只传排除后剩下的文件，jscpd 用 `--ignore`，knip 在项目没有 knip 配置时用临时配置的 `ignore`。
   项目自己有 knip 配置时以项目配置为准，要排除就写进它的 `ignore`。
   嵌套的独立前端（自己有 package.json、不是 workspace）根目录 knip 看不懂，死代码候选会大量误报，先给项目写 knip 配置再信这项指标。
   脚本升级或排除规则改了以后，指标口径跟着变，用 `audit --save-baseline --force` 重存基线，不要拿新旧口径直接比。
3. 把报告整理成问题清单，每条归一类：
   - **真 bug**：能说出复现步骤。
   - **死代码**：工具列出的候选，还没复核。
   - **结构问题**：重复代码、超长文件、模块乱引用。
   - **待定**：模型扫描报的、复现不了的。
4. 热点文件（改得最多）和超长文件、重复代码的交集，标成第一优先级。
5. 报告和清单交给用户看，确认排除目录和优先级。

## 阶段 2 · 建安全网

目标：之后每一次删除、修改、重构，都有自动检查告诉你有没有改坏。

### 2.1 把检查工具装进项目（锁定版本）

装进项目、写进 lock 文件，CI 和本地才会用同一个版本，ratchet 不会因为工具升级而误报。

| 用途 | JS/TS | Python |
|---|---|---|
| 类型检查 | `typescript`（tsconfig 打开 `strict`） | `pyright` |
| 代码检查 | `eslint`（`npm init @eslint/config@latest`） | `ruff` |
| 单元测试 | `vitest` | `pytest` |
| 覆盖率 | `@vitest/coverage-v8` | `pytest-cov` |
| 端到端测试 | `@playwright/test`（`npm init playwright@latest`） | `pytest-playwright` |
| 死代码 | `knip` | `vulture` |

Python 项目用 uv 管理时：`uv add --dev pyright ruff pytest pytest-cov vulture`。

每加一个依赖，在 commit 信息里写一句为什么要加。

### 2.2 存量问题：只许变好

开严格模式后，存量错误可能有几千条。**不要求一次清零，也不要用批量加忽略注释的办法把数字压成 0**：忽略注释会被红绿灯反作弊扫描判为作弊，而且等于把问题藏起来。

做法：开完检查后重跑 `audit --save-baseline --force`，把当前数字定为基线。从此 CI 里跑 `ratchet`，数字只许往下走。

### 2.3 热点文件的现状测试

对第一优先级的文件，先写测试**记录它现在的行为**，对的错的都记，这类测试也叫特征测试。目的不是证明它对，而是之后一改动、行为变了就会报警。

- 优先测纯函数、计算逻辑、数据转换，这些最容易测，也最常出 bug。
- 已知是错的行为，测试里加注释标明"当前行为，已登记为 bug #N"，阶段 4 修的时候再改成正确的期望。
- 写完用 `mutation-check` 抽查几个：把被测代码故意改坏，测试必须翻红，否则这个测试什么都没守住。

### 2.4 Playwright 核心流程（有网页界面时）

```bash
npm init playwright@latest
npx playwright init-agents --loop=claude     # 也可以 --loop=codex / vscode / opencode；Playwright 升级后重跑一次
```

- 先和用户确认 10～20 条核心流程（登录、下单、支付这类坏了就出事的），只写这些，不追求覆盖全部页面。
- 可以让 planner 先出测试计划，用户确认计划写得对，再让 generator 生成代码。
- healer 只许修定位问题（找不到按钮、选择器变了），不许改断言，也不许把测试改成跳过。它的每次改动都要审 diff。
- 定位元素优先用 `getByRole` / `getByTestId`，不用固定等待时间。

### 2.5 提交前检查与 CI

提交前自动跑格式化和快速检查：JS 项目用 lefthook，Python 项目用 pre-commit（用前核对）。

CI 样例（GitHub Actions，放 `.github/workflows/ci.yml`；动作版本号用前到 GitHub Marketplace 核对）：

```yaml
name: ci
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: astral-sh/setup-uv@v6        # ratchet 里的 Python 检查和 jscpd 要用 uvx / npx
      - run: npm ci
      - run: npx tsc --noEmit
      - run: npx vitest run
      - run: npx playwright install --with-deps && npx playwright test
      - run: python3 .governance/gov_audit.py ratchet --root .
```

CI 拿不到 `~/.agents`，所以要把 `gov_audit.py` 复制一份到项目的 `.governance/` 里提交。skill 里的脚本更新后，记得同步这份副本。

**出口**：CI 实际触发过一次，并且结果符合预期；核心流程全绿，或红的都已登记进问题清单。

## 阶段 3 · 删死代码

1. 从体检报告取候选（knip / vulture）。
2. 每条复核，下面这些情况工具会误判成"没人用"：
   - 按字符串注册的路由、命令、插件、事件处理函数
   - 动态 import / `importlib` / 反射
   - 配置文件、模板、定时任务、外部系统回调里引用的
   - 对外发布的接口（库的公开导出、API 端点）
   - 测试 fixture、迁移脚本
   复核方法：全仓库搜名字（包括非代码文件），再看 git log 里最后一次被引用是什么时候。
3. 输出删除清单：文件或函数、判断依据、风险。**交给用户确认。**
4. 按模块分批删，每批：删 → 跑全套测试 → 跑 ratchet → 绿了才提交，一批一个 commit。红了就退回这一批，查原因再定。
5. 删完重跑体检，基线用 `ratchet --update` 下调。

## 阶段 4 · 修 bug

对问题清单里的每个 bug：

1. **复现**：写清复现步骤。复现不了就移到"待定"，不修。
2. **写失败测试**：能用单元测试抓就用单元测试（快、能定位）；只有完整流程才能复现的，用 Playwright。确认它是红的，并且红的原因就是这个 bug。
3. **交给 red-green-mode**：验证命令设成这条测试加上全套测试。按红绿灯的流程走完，最终关卡 `RGM_GATE=PASS` 才算修好。
4. **额外检查**：用 Playwright 的项目，每轮跑一次 `git diff | grep -nE '^\+.*\btest\.(fixme|fail)\b'`，补上红绿灯反作弊目前拦不住的写法。
5. **提交**：一个 bug 一个 commit，信息里写 bug 编号和根因。

同一类 bug 在多处出现时，先看是不是重复代码造成的；是的话先做阶段 5 的合并，再修一次。

## 阶段 5 · 工程化治理

按收益从高到低：

| 治理项 | 做法 | 验证 |
|---|---|---|
| 统一格式 | Prettier / Ruff format 全量跑一次，**单独一个 commit**，不和其他改动混 | 测试全绿 |
| 合并重复代码 | 从体检报告里重复最多的开始，抽成一份 | 测试全绿、重复行数下降 |
| 模块依赖规则 | 写清"哪层可以调哪层"，用 dependency-cruiser（JS）或 import-linter（Python）固化成检查，加进 CI（用前核对） | 规则检查通过 |
| 拆超长文件 | 超过 500 行的按职责拆 | 测试全绿、超长文件数下降 |
| 命名和目录 | 只在确实影响理解时调整；大调整先问用户 | 测试全绿 |

铁律：重构的 diff 里不应该出现测试文件的修改。真需要改测试（比如测试直接引用了被挪走的内部函数），单独提交，并在 commit 信息里说明原因。

## 阶段 6 · 文档

用 `templates/` 里的模板，写到项目根目录和 `docs/`：

| 文件 | 位置 | 说明 |
|---|---|---|
| AGENTS.md | 根目录，另建软链 `CLAUDE.md → AGENTS.md` | Claude Code 和 Codex 都读，只维护一份 |
| ARCHITECTURE.md | `docs/` | 模块地图、数据流、依赖规则 |
| TESTING.md | `docs/` | 测试分层、命令、修 bug 的规矩 |
| decisions/ | `docs/decisions/` | 重要决策，一条一个文件：背景、选择、理由、放弃的方案 |
| HANDOFF.md | 根目录 | 当前状态、下一步 |

要求：

- 写之前派一个只读 agent 把仓库通读一遍，按实际代码写，不凭印象编。
- 文档里的每条命令都实际跑一遍，跑通才写进去。
- AGENTS.md 控制在 100 行以内，细节放到 docs/ 里再链接过去。新 Agent 每次都会读它，太长就没人认真读了。

## 阶段 7 · 长期门禁

- GitHub 分支保护：CI 不通过不能合并（改分支保护规则前先问用户）。
- 规则写进 AGENTS.md：以后每个新 bug 先补失败测试再修；新代码不许让 ratchet 变差。
- 每月重跑一次 `audit`，和上个月的报告对比，看重复代码、死代码、错误数有没有反弹。
- 补充测试管不到的部分：
  - 线上报错监控：Sentry
  - 代码安全扫描：Semgrep；仓库里泄露的密钥：gitleaks（用前核对）
  - 页面样式：Playwright 截图对比 `toHaveScreenshot()`
  - 业务规则（金额、权限、状态流转）：用户亲自确认一遍，写进红绿灯的验收契约

## 检查工具版本升级时

工具升级可能让同样的代码报出更多问题，ratchet 会判"变差"。处理方法：在同一个 commit 上，用新旧两个版本各跑一次 audit。确认增加完全来自工具升级后，用 `audit --save-baseline --force` 重设基线，并在 commit 信息里写明原因。
