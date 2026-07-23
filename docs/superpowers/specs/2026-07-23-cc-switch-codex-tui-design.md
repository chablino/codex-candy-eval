# CC Switch Codex TUI 启动器设计

日期：2026-07-23

状态：用户已确认，待实施计划

## 背景

用户通过 CC Switch App 管理多个 Codex 中转站。App 当前选择为配置 A 且已有 Codex TUI 窗口工作时，用户希望在不切换 App、不影响现有窗口的情况下，用配置 B 启动另一个交互式 Codex TUI。

现有评测脚本的 `--cc-switch-config` 会创建临时 `CODEX_HOME`，适合无状态评测，但不适合交互工作。Codex 官方手册说明 `CODEX_HOME` 是配置、认证、sessions、日志、skills 和其他状态的共同根目录；若按 provider 分离 `CODEX_HOME`，配置 B 无法通过相同工作目录或 session ID 自动找到配置 A 的会话。

本功能保持现有 `CODEX_HOME` 和会话状态不变，只为一次 TUI 启动叠加所选 provider 的配置和认证。因此不同中转站可以同时运行，也可以恢复同一套 sessions。

## 目标

- 新增 `codex_tui.py`，按 CC Switch Codex provider 名称或 ID 启动交互式 Codex CLI。
- 不切换、不写入 CC Switch App 当前选择，也不调用 `cc-switch` CLI。
- 不替换默认 `config.toml` 或 `auth.json`。
- 保持当前 `CODEX_HOME`（默认 `~/.codex`）及其 sessions、SQLite、历史、skills、plugins 和日志。
- 支持把 `--` 后的参数原样传给 Codex，包括 `resume`、session ID、`--model`、`--last` 和普通提示词。
- 同时运行多个不同 provider 的 TUI 时互不覆盖配置或认证。
- 子进程退出后清理本次临时 profile，并保留所有 Codex 会话状态。

## 非目标

- 不改变现有四个评测脚本的临时隔离行为。
- 不为 Claude Code 或 OpenCode 增加交互启动器。
- 不实现 provider 列表、交互选择菜单或 shell 自动补全。
- 不支持 Windows；沿用现有 CC Switch selector 的 macOS/Linux 范围。
- 不保证 `SIGKILL` 后删除临时 profile；文件始终使用 owner-only 权限，后续正常启动可安全忽略遗留的随机 profile。

## 用户接口

启动新 TUI：

```sh
python codex_tui.py --cc-switch-config jianzhile
```

在当前目录打开恢复列表：

```sh
python codex_tui.py --cc-switch-config jianzhile -- resume
```

在任意目录按 session ID 恢复，并覆盖模型：

```sh
python codex_tui.py --cc-switch-config jianzhile -- \
  resume 019f8353-4724-7ec2-8f1b-f101458b5151 --model gpt-5.6-sol
```

`--cc-switch-config` 必填，接受 Codex provider 的显示名称或 ID。精确 ID 优先，名称重复时使用 ID。`--` 只负责分隔启动器参数，不传给 Codex。

启动器保留 Codex 子进程退出码。配置选择、临时 profile 或可执行文件错误使用退出码 `2`。

## 架构

### 配置选择

继续复用 `cc_switch_config.load_provider("codex", selector)`，以只读方式从 `~/.cc-switch/cc-switch.db` 读取一次配置快照。provider ID 只用于选择和诊断，不用于分割 Codex 会话存储。

### 临时 Codex Profile

新增一个只面向 Codex TUI 的 profile runtime：

1. 解析有效 `CODEX_HOME`：优先使用当前环境中的值，否则使用 `~/.codex`。
2. 要求该目录已经存在且是目录，避免启动器意外创建或覆盖用户状态根。
3. 在该目录创建随机名称的 `cc-switch-<random>.config.toml`，权限为 `0600`。
4. 把 provider 的完整 `settings_config.config` 原样写入该 profile。
5. 构造子进程环境副本，把 provider `auth` 中的字符串键值写入环境；当前 CC Switch Codex 配置使用 `OPENAI_API_KEY`。
6. 若环境未显式设置 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`，默认补入 `codex-tui`。
7. 启动 `codex --profile <random> ...`，继续使用原来的 `CODEX_HOME`。
8. 正常退出、异常、Ctrl-C 或 SIGTERM 后只删除本次 profile 文件。

profile 是 Codex 官方支持的用户配置叠加层。它覆盖 App 当前写入基础 `config.toml` 的 model/provider 配置，但不改变基础文件。认证只存在于当前 Python 进程构造的子进程环境，不写入默认 `auth.json`，也不修改父 shell。

### 会话恢复

因为启动器不改 `CODEX_HOME` 和 `CODEX_SQLITE_HOME`，Codex 继续访问原来的 sessions、session 索引和 SQLite 状态：

- 同一工作目录下的 `resume` picker 行为与普通 `codex resume` 一致。
- 不同目录下可以通过 session ID 恢复。
- 用 provider B 恢复 provider A 创建的 session 时，本次 TUI 使用 provider B 的临时 profile 和认证。
- 调用者可以继续传 `--model` 等 Codex 参数，覆盖 profile 中的对应默认值。

### 参数与进程

`codex_tui.py` 使用标准库 `argparse` 解析自身参数，并保留分隔符后的 remainder。启动器通过 `shutil.which` 解析 Codex 可执行文件，使用不捕获 stdin/stdout/stderr 的 `subprocess.run`，让 TUI 直接控制当前终端。

启动器保留自身生成的 `--profile`。若转发参数包含 `--profile`、`--profile=...`、`-p` 或紧凑形式 `-pNAME`，启动前报错，避免所选 CC Switch 配置被第二个 profile 静默替换。其他 `-c/--config` 和 `--model` 参数允许传递，作为调用者的显式覆盖。

## 错误与安全

- provider 缺失、名称歧义、数据库或配置结构错误：请求前退出，沿用脱敏后的 `CcSwitchConfigError`。
- `CODEX_HOME` 缺失、不是目录、不可写，或者 profile 创建/清理失败：输出不含配置正文和认证值的错误。
- auth 必须是可用于环境变量的字符串键值映射；非法键、非字符串值或 NUL 字符在启动前拒绝且不回显值。
- 临时 profile 使用排他创建和 `0600` 权限；随机名称避免并发窗口冲突。
- `os.environ`、CC Switch App、默认 `config.toml`、默认 `auth.json` 和 provider 数据库均不修改。
- provider API key 不进入命令行、日志、repr 或错误文本。

## 测试策略

使用标准库 `unittest` 和 mock，不启动真实 TUI，不发送模型请求。

配置模块测试：

- profile 写入现有 `CODEX_HOME`，内容正确且权限为 `0600`。
- runtime 保留原 `CODEX_HOME`、`CODEX_SQLITE_HOME` 和普通环境变量。
- provider auth 覆盖子进程环境但不修改父环境。
- 默认 originator 为 `codex-tui`，显式值优先。
- 正常、body 异常和 SIGTERM 路径删除本次 profile，但保留现有 sessions/配置/auth。
- 两个并发 runtime 使用不同 profile 名称。
- 非法 auth、无效 `CODEX_HOME` 和文件创建/清理错误被安全转换。

启动器测试：

- parser 要求 selector，正确去掉 `--` 并保留 Codex 参数顺序。
- 调用 `app_type=codex` 的 profile runtime，构造 `codex --profile <name> ...`。
- stdin/stdout/stderr 保持继承，子进程环境来自 runtime。
- `resume`、session ID、`--model` 和无额外参数均正确转发。
- 冲突的 `--profile/-p` 在任何子进程前失败。
- Codex 不存在、配置失败和子进程退出码正确映射。
- README 示例与实际 CLI 一致。

## 验收标准

1. App 当前选择 anyrouter 且已有窗口运行时，可以用 jianzhile 同时启动第二个 Codex TUI，原窗口和 App 选择不变。
2. 新窗口在相同目录运行 `resume` 能看到默认会话列表，在不同目录可按 session ID 恢复。
3. 使用 provider B 恢复 provider A 的 session 时，请求使用 B 的配置与认证。
4. 多窗口并发不共享临时 profile 或认证环境，但共享原 Codex 会话状态。
5. 退出后 sessions 保留，临时 profile 被清理，默认配置、认证和父环境无变化。
6. 完整单元测试、Python 语法检查和 `git diff --check` 通过；自动测试不发送真实请求。
