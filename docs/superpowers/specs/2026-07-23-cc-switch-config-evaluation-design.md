# CC Switch 配置隔离评测设计

日期：2026-07-23

状态：用户已确认，待实施计划

## 背景

`codex-candy-eval` 当前由多个独立脚本直接调用本机的 Codex CLI、Claude Code CLI 或 OpenCode CLI。Codex 与 Claude 脚本默认使用当前用户目录下正在生效的配置，因此通过 CC Switch App 管理多个中转站时，用户必须先切换 App 当前配置，才能对目标中转站进行评测。

本功能让一次评测进程绑定一个指定的 CC Switch App 配置，在隔离临时目录中执行全部测试，不切换 App 当前配置，也不依赖 `cc-switch` CLI。

## 目标

- 为以下四个入口统一增加 `--cc-switch-config NAME_OR_ID`：
  - `codex_candy_eval.py`
  - `codex_tps_eval.py`
  - `codex_juice_eval.py`
  - `claude_candy_eval.py`
- Codex 脚本只查找 CC Switch 中 `app_type = codex` 的配置；Claude 脚本只查找 `app_type = claude` 的配置。
- 配置显示名称是日常入口；名称重名时允许使用 provider ID 消除歧义。
- 以只读方式访问 `~/.cc-switch/cc-switch.db`，兼容当前 App 数据库版本 16，不调用版本不兼容的 `cc-switch` CLI。
- 一个评测进程只加载一次配置快照，并让该进程中的全部请求复用该快照。
- 不传 selector 时保持现有命令、环境、Windows 兼容和单文件管道用法不变。
- 不在命令行、错误表格或日志中泄漏配置正文、API key 或 token。

## 非目标

- 不修改、切换或保存 CC Switch App 的当前配置。
- 不在运行期间动态跟踪 App 中的配置修改。
- 不支持一个评测进程同时使用多个 CC Switch 配置。
- 不为 `opencode_candy_eval.py` 增加 selector；CC Switch 的 Codex/Claude 配置不用于 OpenCode 入口。
- 不为 selector 模式实现 Windows ACL 管理。Windows 上不传 selector 时仍保持现有行为。
- 不让 `curl/wget | python` 单文件模式加载仓库外的共享模块。
- 不增加第三方 Python 依赖。

## 用户接口

四个入口使用同名参数：

```sh
python codex_candy_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high -n 5
python codex_tps_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high
python codex_juice_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol
python claude_candy_eval.py --cc-switch-config anyrouter -m sonnet -r high -n 5
```

脚本类型决定配置类型，不新增 `--provider`：

| 脚本 | CC Switch app_type | 子进程环境 |
| --- | --- | --- |
| `codex_candy_eval.py` | `codex` | `CODEX_HOME` |
| `codex_tps_eval.py` | `codex` | `CODEX_HOME` |
| `codex_juice_eval.py` | `codex` | `CODEX_HOME` |
| `claude_candy_eval.py` | `claude` | `CLAUDE_CONFIG_DIR` |

因此，Codex 和 Claude 中都叫 `anyrouter` 的配置不会冲突。同一个命令中的 `--model`、`--reasoning-effort` 等显式 CLI 参数继续传给原生 CLI，并覆盖配置文件中的相应默认值。

### Selector 规则

1. 查询始终受脚本对应的 `app_type` 限制。
2. 先精确匹配 provider ID。
3. 如果没有 ID 匹配，再精确匹配显示名称。
4. 唯一名称匹配成功时使用该配置。
5. 同类型下存在多个同名配置时，启动失败并列出匹配 ID。
6. 没有匹配项时，启动失败并列出该类型下可用的名称和 ID。

### 单文件管道兼容

现有命令继续可用：

```sh
curl -fsSL "https://raw.githubusercontent.com/haowang02/codex-candy-eval/main/codex_candy_eval.py" | python3 - -m gpt-5.6-sol -r high -n 5
```

四个脚本只在 selector 非空时懒加载共享模块。因此，未指定 `--cc-switch-config` 的单文件运行不受影响。管道模式指定 selector 时，共享模块不在本地，脚本在发送任何请求前退出并提示用户克隆完整仓库运行。

## 架构

### 共享配置模块

新增 `cc_switch_config.py`，集中负责以下安全边界：

- 以 SQLite URI `mode=ro` 打开默认数据库。
- 只读取 `providers` 表中的 `id`、`app_type`、`name` 和 `settings_config`。
- 选择并验证一个 provider 后立即关闭数据库连接。
- 创建私有临时配置目录，产出子进程环境覆盖和待脱敏的 secret 集合。
- 处理正常结束、异常、`KeyboardInterrupt` 和 `SIGTERM` 的清理。
- 提供确定性的文本脱敏函数。

该模块独立存在于本仓库中，不从 `anyrouter-keeper` 的文件路径导入代码，避免两个仓库形成运行时耦合。

### 临时运行环境

Codex provider：

- 将 `settings_config.config` 原样写入 `config.toml`。
- 将 `settings_config.auth` 写入 `auth.json`。
- 仅向评测子进程设置 `CODEX_HOME`。

Claude provider：

- 将完整 `settings_config` 对象写入 `settings.json`。
- 仅向评测子进程设置 `CLAUDE_CONFIG_DIR`。

POSIX 平台上临时目录权限固定为 `0700`，文件权限固定为 `0600`。环境覆盖基于 `os.environ.copy()` 构造，不修改评测进程的父环境。

### 四个评测脚本

每个脚本增加 selector 参数，并在 `main` 中完成一次启动编排：

```text
解析参数
  -> selector 为空：直接进入现有评测循环
  -> selector 非空：懒加载共享模块
       -> 只读选择 provider
       -> 创建临时 runtime
       -> 把 runtime 环境传给全部 subprocess 调用
       -> 评测结束后清理 runtime
```

现有 `run_codex`、`run_claude` 或 `ask` 函数增加可选子进程环境参数。未选择配置时仍向 `subprocess.run` 传 `env=None`，保持系统默认继承语义；选择配置时传入合并后的环境副本。

## 生命周期与信号

配置在进入评测循环前只加载一次。App 后续切换或编辑 provider 不影响已经运行的评测进程；需要重启脚本才能读取新内容。

正常结束、单次评测异常、`Ctrl-C` 和 `SIGTERM` 都必须退出 runtime 上下文并删除临时目录。`SIGTERM` 转换成保留退出码语义的 `SystemExit(128 + SIGTERM)`。无法捕获的 `SIGKILL` 可能留下临时文件，但文件仍保持 POSIX 私有权限。

## 错误与脱敏

数据库缺失、表结构不兼容、配置找不到、名称歧义、配置 JSON 无效或临时环境创建失败属于启动错误：

- 在第一个 Codex/Claude 请求前退出。
- 使用退出码 `2`。
- 错误只包含 selector、provider 类型、结构原因以及可操作的名称/ID。
- 不包含 `settings_config`、auth/env 值或底层异常链中的敏感 payload。

原生 CLI 的非零退出仍按各脚本当前方式显示为评测错误。显示前先对原始 stdout/stderr 使用选中配置的 auth/env 字符串做单次脱敏，再进行换行格式化或预览截断，避免多行 secret 或跨截断边界的 secret 泄漏。

Windows 上传入 selector 时同样在任何请求前以退出码 `2` 失败，并明确说明 selector 模式目前只支持 macOS/Linux。不传 selector 时不导入共享模块，也不改变现有 Windows CLI 解析和执行路径。

## 测试策略

新增标准库 `unittest` 测试，不发送真实模型请求。

共享模块测试：

- ID 优先、唯一名称、同名歧义和缺失选择。
- Codex/Claude 相同名称按 `app_type` 隔离。
- 只读数据库 URI、连接关闭和 schema 校验。
- 非法或非标准 JSON 不回显 payload。
- Codex `config/auth` 与 Claude `env` 的类型校验。
- 临时目录和文件内容、`0700/0600` 权限及退出清理。
- setup、body 和 cleanup 失败的异常优先级。
- secret 最长优先、单次替换、多行和截断前脱敏。

脚本集成测试：

- 四个 parser 都接受 `--cc-switch-config`。
- 三个 Codex 入口把选中 runtime 环境传给每次 `codex` subprocess。
- Claude 入口把选中 runtime 环境传给每次 `claude` subprocess。
- 无 selector 时不访问数据库且 subprocess 行为不变。
- selector 在缺少共享模块的单文件上下文中给出明确启动错误。
- 配置失败不会进入评测循环。
- `SIGTERM` 清理临时 runtime 并保留退出码。
- CLI 失败输出不会泄漏单行、多行或跨截断边界的 secret。

验收时可对真实 App v16 数据库执行“选择 + materialize + 清理”烟测。该烟测不启动 Codex/Claude，也不发送 API 请求。真实评测请求只在用户明确运行评测命令时发生。

## 文档

README 增加：

- 四个脚本的 selector 示例。
- 按脚本自动限定 Codex/Claude 配置类型的说明。
- 名称重名时使用 ID 的说明。
- 启动快照、不切换 App 当前配置和不调用 `cc-switch` CLI 的说明。
- selector 模式的 POSIX 平台范围。
- 单文件管道模式不支持 selector、需要完整仓库的说明。

## 验收标准

1. App 当前选择其他 provider 时，四个目标脚本都能按名称使用指定配置。
2. Codex 与 Claude 的同名配置按脚本类型正确隔离。
3. 一次评测中的全部请求使用同一启动快照。
4. 不传 selector 时，现有本地、Windows 和单文件管道行为不变。
5. 配置错误在请求前失败，错误信息可操作且不泄漏凭据。
6. 正常、异常、`Ctrl-C` 和 `SIGTERM` 路径清理临时配置。
7. 自动测试、编译检查和真实 DB 无请求烟测通过，仓库不包含真实凭据或生成的临时配置。
