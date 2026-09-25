# HearMemory

[English](README.md) | 简体中文

**为多 agent 协同编程设计的共享记忆：并行的子 agent、以及在 Codex、Claude Code、Cursor 之间接力的 agent 共用同一份项目记忆，由又小又快的判断模型 Jev 拿真实证据（测试结果、代码改动、提交记录）核对每个 agent 的说法，让下一个 agent 知道哪些能信。**

HearMemory 让在同一个项目里干活的多个编程 agent 共用一份记忆。它解决两种情况：一是几个子 agent
同时干活，彼此看不到对方发现了什么；二是一个 agent 把活交给另一个，比如先让 Codex 写代码，再让
Claude Code 来检查。HearMemory 会记下 agent 做了什么（跑了哪些命令、测试结果、改了哪些文件）和说了
什么（"我修好了 X"、"测试通过了"），再拿记录下来的证据去核对每一句说法，然后在每个新会话开始时给
一段简短的简报：别的 agent 发现了什么，哪些说法有证据，哪些有争议或已经被证明是错的。

**名字的由来：** agent 对自己工作的说法，在有证据之前都只是"传闻"（英文 hearsay）。HearMemory
负责保存记忆，并告诉你其中哪些部分有证据支持，让下一个 agent 知道哪些能信。

状态：alpha（0.1.0）。支持 macOS 和 Linux，需要 Python 3.11 或更新版本。

## 目录

- [两分钟上手](#两分钟上手)
- [安装](#安装)
- [接入你的 agent](#接入你的-agent)
- [简报长什么样](#简报长什么样)
- [Jev 判断器和 API 密钥](#jev-判断器和-api-密钥)
- [HearMemory 会写哪些文件](#hearmemory-会写哪些文件)
- [卸载](#卸载)
- [局限](#局限)
- [是怎么测试的](#是怎么测试的)
- [许可证](#许可证)

## 两分钟上手

```sh
cd your-project                       # 要用提交前检查的话，项目得是 git 仓库
python3 -m venv .venv                 # 项目已有虚拟环境的话直接用它
.venv/bin/pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
.venv/bin/hearmemory init --hosts claude,codex,git

sh .hearmemory/host/claude/launch.sh     # 带着 HearMemory 启动 Claude Code（只对这一次会话生效）
sh .hearmemory/host/codex/launch.sh      # 或者带着 HearMemory 启动 Codex
```

然后照常干活。新会话开始时，Claude Code 会自动收到简报；Codex 会按 `AGENTS.md` 里的说明先去读
简报。你自己也可以随时查看记忆：

```sh
.venv/bin/hearmemory recall --brief      # 看新会话会收到的简报
.venv/bin/hearmemory recall "div by zero"  # 搜索记忆
.venv/bin/hearmemory status              # 记录数量、判断器状态、今天花了多少钱
```

没有 API 密钥也能用，只是跳过需要模型的判断（见 [Jev 判断器](#jev-判断器和-api-密钥)）。

## 安装

把 HearMemory 装进你要用它的那个项目的虚拟环境里：

```sh
.venv/bin/pip install "hearmemory[jev] @ git+https://github.com/ssd1051/hearmemory"
```

- 目前请从 GitHub 安装，之后可能会发布到 PyPI。
- 装好后有两个命令：`hearmemory` 和简写 `hmem`，两者完全一样。
- `[jev]` 会顺带装上 `typesafe-sdk`（来自 PyPI），也就是 Jev 判断器的客户端。只想用纯规则模式可以不加：
  `pip install "hearmemory @ git+https://github.com/ssd1051/hearmemory"`。
- 核心部分没有别的依赖（只用 Python 标准库）。
- `hearmemory init` 会把这个 Python 的完整路径写进生成的 hook 和脚本里。虚拟环境挪了位置或删掉的话，
  重新运行一次 `hearmemory init`。

## 接入你的 agent

`hearmemory init --hosts ...` 用来接入一个或多个"宿主"（host）。默认是 `claude,codex,git`；`cursor` 要
明确写出来才会装。所有设置都只写在项目里面（见 [HearMemory 会写哪些文件](#hearmemory-会写哪些文件)）。

### Claude Code

```sh
sh .hearmemory/host/claude/launch.sh     # 等同于 claude --mcp-config ... --settings ...（多余的参数会原样传给 claude）
hearmemory host claude-cmd               # 想自己运行 claude 的话，这条命令会打印完整的启动命令
```

启动脚本只为这一次会话加载 HearMemory 的 MCP 服务和 hook，不会改你的 Claude Code 设置。这些 hook
会记下工具的结果（命令、改动、子 agent 的结果），给每个新会话和每个新的子 agent 一段简报，并在
`git commit` 执行前做一次检查。如果想让直接运行 `claude` 时也总是加载 HearMemory，用
`hearmemory init --claude-persist`：它会往项目自己的 `.mcp.json` 和 `.claude/settings.local.json` 里加条目。

### Codex

```sh
sh .hearmemory/host/codex/launch.sh      # 带着 HearMemory 的 MCP 服务启动 codex（多余的参数会原样传给 codex）
hearmemory host codex-cmd                # 只打印 `codex -c ...` 启动命令
```

- `hearmemory init` 会在 `AGENTS.md` 里加一小段带标记的说明，告诉 Codex：开始时先读简报，得出结论时记下
  来，提交前运行 `hearmemory check`。
- Codex 没有"不改用户配置就能用"的逐个工具 hook，所以 HearMemory 读 Codex 自己的会话记录
  （`~/.codex/sessions`，只读）来了解 Codex 做了什么。读取的时机：启动脚本退出时、后台 worker 大约
  每分钟一次、以及每次调用 hearmemory 工具之前。`hearmemory import codex` 可以手动读取。在 `hearmemory init`
  之前就已结束的会话会被跳过，除非加 `--since <时间>` 或 `--all-history`。
- 注意：Codex 以 `workspace-write` 模式第一次在某个目录里运行时，**是 Codex 自己**往
  `~/.codex/config.toml` 里加一条 `[projects."<路径>"]` 信任记录。HearMemory 从不写那里。想保持干净的话
  可以手动删掉这一条。

### Cursor（实验性）

```sh
hearmemory init --hosts cursor           # 可以和别的一起装：--hosts claude,codex,cursor,git
```

这会在项目里写 `.cursor/mcp.json`、`.cursor/hooks.json` 和 `.cursor/rules/hearmemory.mdc`。
**Cursor 的适配代码已经写好并有单元测试，但还没有在真实使用中测试过。** 不同版本的 Cursor，hook
的字段名可能不一样。欢迎反馈问题。

### git 提交前检查（对所有 agent 和你自己都有效）

`git` 这个宿主会装一个 pre-commit hook。每次提交前，它拿暂存的改动去对照记忆：是不是依赖了已经被
推翻的说法？有没有相关的未解决问题？相关的测试上一次跑是不是失败了？默认只打印提醒，提交照常进行。
可以在 `.hearmemory/config.toml` 的 `[precommit]` 里按宿主改成 `off`、`warn`、`hold_once`（第一次先拦
一下）或 `block`（拦截）。原来就有的 pre-commit hook 会保留，并且先运行。设置环境变量
`HEARMEMORY_DISABLE=1` 可以让某一条命令跳过所有 hook。

### 手动使用，或在其它工具里用

```sh
hearmemory record --kind claim "src/calc.py div() raises ValueError when b == 0"
hearmemory record --kind issue "tests/test_calc.py has no test for negative numbers"
hearmemory recall "div"                  # 搜索
hearmemory check --staged                # 和 git hook 做的检查一样
hearmemory issues                        # 未解决的问题
```

支持 MCP 的 agent 可以把同样的功能当作工具来用：`hearmemory_recall`、`hearmemory_record`、`hearmemory_check`、
`hearmemory_issues`、`hearmemory_status`。

## 简报长什么样

新会话会收到这样一段简报（大约控制在 600 个 token 以内）：

```text
[hearmemory] Shared project memory — 5 items. Format: status · claim · source.
Refuted / disputed:
- [REFUTED] "tests/test_calc.py passes with the new div()" — codex · session 01a0d40a · 2h ago · 8761b49
    counter-evidence: test `pytest tests/test_calc.py` failed (1 failed, 5 passed) (claude · session 11f0b146 · subagent general-purpose · 1h ago · 8761b49)
Open issues:
- [ISSUE i-6f82] tests/test_calc.py has no test for negative numbers
New from other agents:
- test `pytest` passed (6 passed) — codex · session 01a0d411 · 2m ago · 76860d9+dirty → cf15004
- [SUPPORTED] "Added div(a, b); it raises ValueError when b == 0; python -m pytest -q passes (3 passed)." — codex · session 01a0d40a · 10m ago · 8761b49
- [ADDRESSED?] "Tests for div only cover positive numbers." → codex session 01a0d411 edited tests/test_calc.py, 6 passed, cf15004 — claude · session 11f0b146 · subagent general-purpose · 6m ago · 8761b49
Details: hearmemory_recall (MCP) or `hearmemory recall <query>`. Before committing: hearmemory_check / `hearmemory check --staged`.
```

每一行最后写着出处：哪个工具、哪个会话、哪个子 agent、多久以前、哪个 git 提交
（`abc1234+dirty → def5678` 的意思是"在 abc1234 上有未提交的改动，后来提交成了 def5678"）。

各个标签的意思：

| 标签 | 意思 |
|---|---|
| `[SUPPORTED]` 有证据支持 | 有来自说法作者以外的证据支持它（比如另一个 agent 跑的测试）。 |
| `[WEAK SUPPORT]` 弱支持 | 判断器倾向于"支持"，但把握不大（默认置信度低于 0.65）。 |
| `[SAME-SOURCE ONLY]` 仅同源转述 | 唯一的支持来自作者自己的输出。说得通，但没人独立核实过。 |
| `[UNVERIFIED]` 待确认 | 还没判断（没有密钥、还没处理到，或者没有能拿来对照的证据）。 |
| `[INSUFFICIENT]` 证据不足 | 判断过了，但现有证据既不能支持也不能推翻它。这不等于它是错的。 |
| `[DISPUTED]` 有争议 | 既有支持它的证据，也有反对它的证据。会同时开一个问题（issue）。 |
| `[REFUTED]` 已被反驳 | 有证据和它矛盾。旁边一定会同时列出反证。 |
| `[OUTDATED]` 已过时 | 它描述的是某个测试结果，之后代码改了，结果也变了。 |
| `[ADDRESSED?]` 可能已处理 | 有人报告的问题，之后可能已被另一个 agent 处理了（改了相关文件，测试也通过了）。这是按规则猜的，请自己确认。 |
| `[ISSUE ...]` 未决问题 | 一个还没解决的问题：手动记的，或者因为争议自动开的。 |
| `[ARCHIVED]` 已归档 | 旧的、已经不相关的内容。只有 `hearmemory recall --include-archive` 才显示。任何记录都不会被删除。 |

说法后面的"(the agent's own report)"表示这句话是 agent 自己写的汇报。在 `.hearmemory/config.toml` 里
设置 `brief.lang = "zh"`，简报的标签和说明就会变成中文（比如 `[有证据支持]`、`[已被反驳]`）。

## Jev 判断器和 API 密钥

HearMemory 会向 TypeSafe 的一个又小又快的判断模型 **Jev** 问四类很窄的问题：两处提到的是不是同一个
代码对象；两条记录是不是同一件事；一句说法是不是在重复另一句；记录下来的证据是支持还是推翻某句
说法。其余的都由普通规则决定。

- **密钥：** 在启动 agent 的那个 shell 里设置 `TYPESAFE_API_KEY`（启动脚本会从这个 shell 启动后台
  worker）。密钥只从环境变量读取，从不写到磁盘上。
- **没有密钥：** HearMemory 照样能用。确定性规则能判的照判（比如测试现在失败了，就推翻"测试通过"这句
  说法）；其它问题先等着，对应的说法显示为 `[UNVERIFIED]`。不会出错，也不花钱。
- **会发给 Jev 什么：** 只发回答一个问题所需的几小段文字：说法那一句，加上最多 3 段证据，每段不超过
  800 个字符（一段 diff 摘录、一行测试汇总），再加上"codex session at 2026-09-24T10:00Z, commit
  a1b2c3d"这样的出处标注。从不发整个文件，也从不发完整日志。内容在存盘前脱敏一次，发出前再脱敏
  一次。匹配 `privacy.exclude_globs` 的文件（比如 `.env`、`*.pem`、`*secret*`）的内容根本不会被记录。
  `privacy.jev_exclude_globs` 可以指定哪些路径的内容不发给 Jev；`privacy.send_to_jev = false` 可以
  完全关掉 Jev。
- **花费上限（默认值，在 `.hearmemory/config.toml` 的 `[jev]` 里）：** 每天（按 UTC 算）最多 200 次调用、
  最多 0.05 美元，后台 worker 每跑一轮最多 40 次。每次调用只有几百个 token。同一个问题不会付两次钱
  （结果有缓存）。`hearmemory status` 会显示今天花了多少。
- 判断在后台 worker 里做，从不在 agent 自己的回合里做，所以不会拖慢 agent。

## HearMemory 会写哪些文件

HearMemory 只在项目里面写文件。它从不写 `~/.claude`、`~/.codex`、`~/.cursor`、你的全局 git 配置，也不写
任何用户级的位置。

| 宿主 | 文件 |
|---|---|
| 全部 | `.hearmemory/`（记忆、配置、日志；会加进 `.git/info/exclude`，git 会忽略它） |
| claude | `.hearmemory/host/claude/{mcp.json,settings.json,launch.sh}`；用 `--claude-persist` 时还有 `.mcp.json` 和 `.claude/settings.local.json` |
| codex | `AGENTS.md` 里一段带标记的说明（没有这个文件就新建），`.hearmemory/host/codex/launch.sh` |
| cursor | `.cursor/mcp.json`、`.cursor/hooks.json`、`.cursor/rules/hearmemory.mdc` |
| git | `.git/hooks/pre-commit`（原有的 hook 会保留并串起来运行）、`.hearmemory/host/git/pre-commit` |

每一处改动都记在 `.hearmemory/install_manifest.json` 里，所以能精确撤销。唯一的例外不是 HearMemory 做的：
Codex 可能会往 `~/.codex/config.toml` 里加一条信任记录（见 [Codex](#codex)）。

## 卸载

```sh
.venv/bin/hearmemory uninstall                 # 删掉 init 加的 hook 和文件，保留记忆
.venv/bin/hearmemory uninstall --purge --yes   # 连 .hearmemory/（记忆本身）一起删掉
.venv/bin/pip uninstall hearmemory
```

`uninstall` 会先停掉后台 worker，再删掉 `AGENTS.md` 里的那段说明、合并进去的 JSON 条目和 git hook，
并把原来的 pre-commit hook 恢复回去。你手动改过的生成文件不会被删，而是移到 `.hearmemory/archive/`。

## 局限

- **只支持 macOS 和 Linux。** HearMemory 用 `fcntl` 文件锁，不支持 Windows。
- **Cursor 支持是实验性的**（代码写好了，还没在真实使用中测试过）。
- **Codex 的接入依赖读取 Codex 的会话记录。** 如果 Codex 改了记录格式，在 HearMemory 更新之前导入可能会
  出问题。
- **`[ADDRESSED?]` 是按规则猜的**，不是判断器的结论。它只表示"报告之后，有人改了提到的文件，而且测试
  通过了"。
- 从 agent 的话里找出"说法"、决定问判断器什么问题，靠的是启发式规则：有些说法会漏掉，有些问题问了
  也没用。
- 相关度只看共同的文件路径和标识符，不懂意思。措辞不同的相关条目可能进不了简报，但用
  `hearmemory recall` 能搜到。
- 记忆按"一台机器上的一个项目目录"共享，不会在多台机器之间同步。
- 密钥脱敏靠模式匹配。长得不像任何已知格式、又不在排除文件里的秘密可能漏过。
- 记忆可能比最新的操作晚几秒；这时简报里会写"memory as of ..."（记忆截至某时刻）。

## 是怎么测试的

- 一套离线测试，大约 630 个：每个部分的单元测试；在临时 git 仓库里跑真实命令行、MCP 服务、hook 和
  git hook 的端到端测试；还有回放测试。整套测试不需要网络，也不需要 API 密钥；只有设置了密钥时才会
  跑一个很小的 Jev 在线测试。
- 在一个小测试项目上做了五次真实的多 agent 运行：Claude Code 桌面版带并行子 agent（包括在 git
  worktree 里的子 agent）、Codex 命令行，以及 Codex → Claude Code → Codex 这样的交接。每次运行中发现
  的问题都修好了，并用这次运行脱敏后的记录做成回放测试固定下来（`tests/test_replay_*.py`）。

## 开发

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

架构见 [docs/DESIGN.md](docs/DESIGN.md)（英文），更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。
