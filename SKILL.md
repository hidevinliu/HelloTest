---
name: project-governance
version: 0.1.3
description: >-
  中大型项目（尤其是 AI 编程写出来的）从"修 bug 像打地鼠"走到"能持续维护"的治理流程：体检 → 建测试安全网 →
  删死代码 → 修 bug → 工程化治理 → 交接文档 → 长期门禁。自带体检脚本和"只许变好"检查（gov_audit.py）。
  用户说「项目治理 / 体检一下这个项目 / 治理一下这个仓库 / bug 修不完 / 修了又冒新的 / 打地鼠 /
  清理死代码 / 代码太乱要工程化 / 给项目补测试和文档 / 让新 Agent 能接手这个项目 / 继续治理」时使用。
  只修一个具体 bug 用 systematic-debugging；只想把现有测试跑到全绿用 red-green-mode；写需求文档用 AI-PRD-Devin。
---

# 项目治理（project-governance）

## 这是什么

把一个已经写了很多代码、bug 反复冒出来的项目，按 7 个阶段治理成"改了能验证、交接能看懂"的状态。

- **体检和"只许变好"由脚本裁决**：`scripts/gov_audit.py`，零依赖，只用 Python 标准库。
- **修 bug 由红绿灯裁决**：阶段 4 直接用 `red-green-mode`，本 skill 不重写它的循环和反作弊。
- **测试有没有真在测**：用 `mutation-check`（或红绿灯的 `rgm_mutation.py`）。
- 本 skill 只管阶段顺序、每个阶段的出口条件，以及哪些事必须先问用户。

## 每次进入先做这一步

读 `<项目>/.governance/PROGRESS.md`：

- **存在** → 从里面写的"当前阶段"继续，先把上一个阶段的验证命令重跑一遍，确认起点和记录一致。
- **不存在** → 新开一轮治理：向用户确认三件事（项目路径；有没有线上用户在用；有没有不能碰的目录或功能），然后从阶段 0 开始，并用 `templates/PROGRESS.md` 建这个文件。

每个阶段结束时更新 PROGRESS.md：阶段、完成证据（命令、退出码、commit）、下一步。会话中断也写，写清楚卡在哪里。

## 7 个阶段

| 阶段 | 做什么 | 出口条件（有证据才算） | 要用户确认的事 |
|---|---|---|---|
| 0 准备 | 新分支 + 基线标签；装依赖、启动项目；跑一遍现有测试 | 项目能启动；现有测试的红绿数已记录 | 无 |
| 1 体检 | `gov_audit.py audit --save-baseline`，只读不改代码 | 体检报告 + 问题清单（真 bug / 死代码 / 结构问题 / 待定） | 看报告，确认排除目录和优先级 |
| 2 安全网 | 静态检查、热点文件的现状测试、Playwright 核心流程、提交前检查、CI（含 ratchet） | CI 跑得起来；核心流程绿，或红的已登记 | 核心流程清单（10～20 条） |
| 3 删死代码 | 工具列候选 → 逐条复核 → 清单给用户 → 分批删，每批跑全套测试 | 每批一个 commit，全套测试绿，ratchet 通过 | **删除清单必须先确认** |
| 4 修 bug | 每个 bug：复现 → 写失败测试 → red-green-mode 修 → 最终关卡 PASS → 提交 | 问题清单里的 bug 每条都有测试；红绿灯 `RGM_GATE=PASS` | 修复会改变业务规则时 |
| 5 工程化 | 统一格式、合并重复代码、模块依赖规则、拆超长文件 | 测试一行没改且全绿；ratchet 指标下降 | 目录结构大调整时 |
| 6 文档 | 用 `templates/` 写 AGENTS.md、ARCHITECTURE.md、TESTING.md、HANDOFF.md | 文档里的每条命令都实际跑通过 | 无 |
| 7 长期门禁 | CI 必过才能合并；新 bug 先补测试；每月重跑体检 | 门禁配置已提交并实际触发过一次 | 改分支保护规则时 |

阶段 4 和 5 可以交替进行：同一个 bug 在多处重复出现时，先合并重复代码，再修一次。

每个阶段的具体命令、按技术栈的工具选择、CI 配置样例 → `references/phases.md`。

## 硬规则

1. **没有安全网不删、不重构。** 阶段 2 的出口没过，不进阶段 3 和 5。
2. **删除先确认。** 死代码清单交给用户确认后再删。工具会误判按字符串注册的路由、动态加载、配置文件引用的代码。
3. **修 bug 先有红测试。** 复现不了的 bug 进"待定"，不修。模型扫描出来的问题只当线索。
4. **重构不改测试。** 阶段 5 只改结构不改行为，测试文件出现在重构 diff 里就要停下来查原因。
5. **只许变好。** 存量问题不要求一次清零，但 `gov_audit.py ratchet` 必须退出码 0：任何指标都不能变多。退出码 2（没测出来）不等于通过。
6. **不碰线上。** 全程在分支上做；客户正在用的环境不拿来试验。
7. **没有证据不报完成。** 每个阶段的出口写进 PROGRESS.md，要有命令和退出码；工具没跑起来就如实写"未验证"。
8. **Playwright 的反作弊缺口。** red-green-mode v0.24.0 的 `rgm_anticheat.py` 拦得住 `test.skip()`，拦不住 `test.fixme()`（2026-10-09 实测）。用 Playwright 时每轮额外检查一次：`git diff | grep -nE '^\+.*\btest\.(fixme|fail)\b'`，有命中就当作弊处理。Playwright 的 healer 会在它认为功能坏了的时候把测试改成跳过，它的改动要逐个审。

## 常用命令

```bash
GOV=~/.agents/skills/project-governance/scripts/gov_audit.py

# 阶段 1：体检并存基线（报告写到 <项目>/.governance/，不改代码）
python3 $GOV audit --root <项目> --save-baseline
# 有生成代码、测试样本、未跟踪的设计产物等目录时排除掉；排除规则会存进基线，并传给 ruff / pyright / knip / jscpd
python3 $GOV audit --root <项目> --exclude generated tests/fixtures --save-baseline

# 之后每次提交前、CI 里：只许变好（0 通过 / 1 变差 / 2 无法判断）
python3 $GOV ratchet --root <项目>
# 指标变少后把基线往下调（只降不升）
python3 $GOV ratchet --root <项目> --update
```

体检会用 `npx` / `uvx` 临时运行检查工具，第一次要联网下载。JS 项目先装好依赖（node_modules）再体检，否则类型检查和死代码结果不准。

## 文件

| 文件 | 用途 |
|---|---|
| `scripts/gov_audit.py` | 体检与"只许变好"检查 |
| `tests/test_gov_audit.py` | 脚本自测：`python3 -m unittest discover -s tests` |
| `references/phases.md` | 每个阶段的详细做法、按技术栈的工具、CI 样例 |
| `templates/PROGRESS.md` | 治理进度（放 `<项目>/.governance/`） |
| `templates/AGENTS.md` | 新 Agent 进项目第一个读的文件 |
| `templates/ARCHITECTURE.md` | 模块地图与依赖规则 |
| `templates/TESTING.md` | 测试分层与验证命令 |
| `templates/HANDOFF.md` | 交接：当前状态、下一步 |
