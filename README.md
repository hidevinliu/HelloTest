# HelloTest

**给 AI 写出来的项目做「体检」，再守住「只许变好」。**

AI 编程写得很快，项目大了以后常常变成「修 bug 像打地鼠」：类型错误、死代码、重复代码越积越多，改一处冒一处。HelloTest 做两件事：

1. **体检**：自动识别项目的技术栈，调用现成的检查工具，把结果汇总成一份中文报告和一组数字指标。
2. **只许变好**：把这次体检的数字存成基线。以后每次提交都重测一遍，任何一项指标变多就判失败。存量问题不要求一次清零，但不许再变多。

它是一个 Python 脚本（`scripts/gov_audit.py`，只用标准库，不用装依赖），外加一套 7 个阶段的治理流程。这套流程写成了 Claude Code / Codex 能直接读的 skill（`SKILL.md`）。

> 在本机，这个 skill 的内部名字是 `project-governance`，对 agent 说「体检一下这个项目」就会触发。

## 用法

```bash
# 体检：不改代码，报告写到 <项目>/.governance/
python3 scripts/gov_audit.py audit --root <项目> --save-baseline

# 排除生成代码、测试样本等目录（排除规则会存进基线）
python3 scripts/gov_audit.py audit --root <项目> --exclude generated tests/fixtures --save-baseline

# 每次提交前、CI 里：只许变好
python3 scripts/gov_audit.py ratchet --root <项目>
#   退出码 0 = 没有指标变多，可以合并
#   退出码 1 = 有指标变多，拒绝合并
#   退出码 2 = 有指标这次没测出来，无法判断（不算通过）

# 指标变少以后，把基线往下调（只降不升）
python3 scripts/gov_audit.py ratchet --root <项目> --update
```

检查工具通过 `npx` / `uvx` 临时运行，第一次要联网下载。项目里已经装了的，优先用项目自己的版本。JS 项目先装好依赖（`node_modules`）再体检，否则类型检查和死代码的结果不准。

## 查哪些指标

全部是「越少越好」：

| 指标 | 用什么工具查 |
|---|---|
| TypeScript 类型错误 | TypeScript（`tsc --noEmit`） |
| ESLint 问题（错误 + 警告） | ESLint |
| SwiftLint 问题（错误 + 警告） | SwiftLint |
| JS/TS 死代码候选（没用到的文件、导出、依赖） | Knip |
| npm 高危和严重漏洞 | `npm audit` / `pnpm audit` |
| Python 类型错误 | Pyright |
| Python lint 问题 | Ruff |
| Python 死代码候选 | Vulture |
| Python 依赖的已知漏洞 | pip-audit（uv/poetry 项目先经 export 转换） |
| 重复代码行数 | jscpd |
| 超过 500 行的代码文件数 | 脚本自己数 |

项目里没有对应语言的，就跳过那一项。工具没跑起来时，报告里如实写「失败」，不会当成 0。

## 用了谁的东西

**HelloTest 自己的代码只有脚本、测试、流程文档和模板。** 下面这些工具都不在这个仓库里，也不随它分发；体检时脚本在你的电脑上调用它们，版权归各自作者，按各自的许可证使用：

| 工具 | 作者 | 许可证 | 在 HelloTest 里做什么 |
|---|---|---|---|
| [TypeScript](https://github.com/microsoft/TypeScript) | Microsoft | Apache-2.0 | 类型检查 |
| [Pyright](https://github.com/microsoft/pyright) | Microsoft | MIT | Python 类型检查 |
| [Playwright](https://github.com/microsoft/playwright) | Microsoft | Apache-2.0 | 流程第 2 阶段推荐用它写核心流程测试；脚本本身不调用 |
| [ESLint](https://github.com/eslint/eslint) | OpenJS Foundation 及贡献者 | MIT | JS/TS lint |
| [SwiftLint](https://github.com/realm/SwiftLint) | Realm（现属 MongoDB）及贡献者 | MIT | Swift lint |
| [Knip](https://github.com/webpro-nl/knip) | Lars Kappert 及贡献者 | ISC | JS/TS 死代码 |
| [jscpd](https://github.com/kucherenko/jscpd) | Andrey Kucherenko 及贡献者 | MIT | 重复代码 |
| [Ruff](https://github.com/astral-sh/ruff) | Astral | MIT | Python lint |
| [Vulture](https://github.com/jendrikseipp/vulture) | Jendrik Seipp 及贡献者 | MIT | Python 死代码 |
| [pip-audit](https://github.com/pypa/pip-audit) | PyPA | Apache-2.0 | Python 依赖漏洞 |
| npm / pnpm 的 `audit` | npm（GitHub）/ pnpm | 随各自的包管理器 | JS 依赖漏洞 |

许可证是 2026-10-18 在 npm、PyPI 和 GitHub 上查到的，以各项目当前的声明为准。

已核对过输出格式的（2026-10-09 实测，swiftlint 为 2026-10-23 补测）：`tsc`、`knip --reporter json`、`jscpd --reporters json`、`npm audit --json`、`ruff --output-format json`、`pyright --outputjson`、`vulture`、`pip-audit -f json`。还没实测过的：`eslint -f json`、`pnpm audit --json`。这两项解析失败时，报告会写「失败」。

## 7 个阶段的治理流程

| 阶段 | 做什么 |
|---|---|
| 0 准备 | 开新分支、打基线标签；把项目跑起来；现有测试跑一遍并记下红绿数 |
| 1 体检 | `gov_audit.py audit --save-baseline`，只读不改，整理问题清单 |
| 2 安全网 | 静态检查、热点文件测试、Playwright 核心流程、提交前检查、CI |
| 3 删死代码 | 工具列候选，逐条复核，**清单先给人确认**，再分批删 |
| 4 修 bug | 先写能复现的失败测试，再修，修完过最终关卡 |
| 5 工程化 | 合并重复代码、拆超长文件、定模块依赖规则；测试一行不改 |
| 6 文档 | 用 `templates/` 写 AGENTS.md、ARCHITECTURE.md、TESTING.md、HANDOFF.md |
| 7 长期门禁 | CI 必须通过才能合并；每月重新体检一次 |

每个阶段的具体命令、按技术栈怎么选工具、CI 配置样例，见 [`references/phases.md`](references/phases.md)。

## 和我的其他工具一起用

- [AI-PRD-Devin](https://github.com/hidevinliu/AI-PRD-Devin)：先把需求写清楚
- [red-green-mode](https://github.com/hidevinliu/red-green-mode)：修 bug 的阶段用它跑「红 → 绿」循环，并检查 AI 有没有作弊让测试变绿
- [mutation-check](https://github.com/hidevinliu/red-green-mode/tree/main/skills/mutation-check)：故意改坏代码，检查测试会不会变红

HelloTest 不依赖它们，单独也能用。

## 装成 agent skill

```bash
git clone https://github.com/hidevinliu/HelloTest.git ~/.agents/skills/project-governance
# Claude Code 用户也可以放到 ~/.claude/skills/project-governance
```

## 自测

```bash
python3 -m unittest discover -s tests
```

## 许可证

[MIT](LICENSE)。上面列出的第三方工具按它们各自的许可证使用。

---

### English summary

HelloTest gives an AI-written codebase a health check and then enforces a "no new problems" ratchet. `scripts/gov_audit.py` (Python standard library only) detects the stack, runs existing checkers (TypeScript, Pyright, ESLint, SwiftLint, Knip, jscpd, Ruff, Vulture, pip-audit, npm/pnpm audit), writes a report, and stores the counts as a baseline. `ratchet` fails (exit 1) if any count goes up, and returns exit 2 when a metric could not be measured. None of those tools are bundled: they are invoked on your machine and remain under their own licenses (listed above). `SKILL.md` describes a 7-stage governance process that coding agents such as Claude Code and Codex can follow. MIT licensed.
