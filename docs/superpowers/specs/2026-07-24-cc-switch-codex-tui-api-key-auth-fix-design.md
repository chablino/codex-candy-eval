# CC Switch Codex TUI API-key 认证修复设计

> **Superseded:** The provider CLI override mechanism in this document is replaced by
> `docs/superpowers/specs/2026-07-24-codex-tui-profile-auth-rewrite-design.md` after
> Codex 0.145.0 compatibility testing exposed provider-map layering behavior.

日期：2026-07-24

状态：用户已确认设计，待实施计划

本设计修正
`docs/superpowers/specs/2026-07-23-cc-switch-codex-tui-design.md`
中“仅注入环境变量即可覆盖认证”的假设；其余共享会话、临时 profile 和进程生命周期设计
继续有效。

## 问题

`codex_tui.py --cc-switch-config wuming` 已能通过临时 profile 把请求地址切换到 wuming，
但当 CC Switch App 当前选择另一个 provider 时，请求返回 `401 Invalid token`。同一个 wuming
配置通过评测脚本运行正常，切换 App 后直接运行 Codex TUI 也正常。

根因是配置与认证走了不同路径：

- 评测脚本创建隔离 `CODEX_HOME`，把所选 provider 的 `config.toml` 和 `auth.json` 一起写入，
  因而配置和 token 始终匹配。
- 交互启动器保留共享 `CODEX_HOME`，只通过 profile 覆盖 provider 配置，并把
  `OPENAI_API_KEY` 写入子进程环境。
- 当前 CC Switch 中转站配置使用 `requires_openai_auth = true` 且没有 `env_key`。Codex
  0.145.0 在这种模式下从共享 `CODEX_HOME/auth.json` 读取 token，不读取环境中的
  `OPENAI_API_KEY`。
- 因此 App 选择 provider A、启动器选择 provider B 时，请求会把 A 的 token 发往 B 的
  地址，产生 401。

本机 CC Switch 数据库中的 29 个 API-key 中转站均使用单一 `OPENAI_API_KEY`；其中 28 个
provider ID 为 `custom`，另一个为 `hlool`。`OpenAI Official` 使用包含登录 tokens 的复杂
认证结构，不属于本次修复范围。

## 目标

- App 当前选择 provider A 时，`codex_tui.py --cc-switch-config B` 必须使用 B 的
  `OPENAI_API_KEY`。
- 继续共享当前/default `CODEX_HOME`、sessions、SQLite、历史、skills 和 plugins。
- 不修改共享 `auth.json`、默认 `config.toml`、CC Switch 数据库或父进程环境。
- 不影响已运行的其他 Codex 窗口。
- token 只出现在所启动 Codex 子进程的环境中，不出现在命令行、profile、日志、错误或
  对象 `repr` 中。
- 保持原有 `resume`、session ID、`--model` 和用户 `-c/--config` 参数转发行为。

## 范围外

- 不为 `OpenAI Official` 的 ChatGPT/API 登录 tokens 增加临时认证切换。
- 不创建影子或 provider 专属 `CODEX_HOME`。
- 不临时替换、锁定或恢复共享 `auth.json`。
- 不改变评测脚本的隔离运行时。
- 不改变 Claude Code 支持。

## 方案比较

### 方案 1：Codex CLI 配置覆盖（采用）

解析所选配置，识别活动 provider ID，并在 Codex 命令中加入两个不含 token 的 `-c` 覆盖：

```text
model_providers."<provider-id>".requires_openai_auth=false
model_providers."<provider-id>".env_key="OPENAI_API_KEY"
```

最终解析后的 provider 改为从子进程环境读取所选 key。这是 Codex 官方支持的自定义
provider 认证机制，保留共享状态且无需写认证文件。

### 方案 2：重写临时 profile（不采用）

可以在 profile 内修改 `requires_openai_auth` 并增加 `env_key`，但 Python 标准库只有 TOML
读取器，没有可靠的 TOML 写回器。文本替换对表顺序、注释和未来 CC Switch 配置格式脆弱。

### 方案 3：影子 `CODEX_HOME`（不采用）

可以写入临时 `auth.json`，再用符号链接共享 sessions 和其他状态。该方案难以覆盖 Codex
当前及未来所有根目录状态文件，容易出现部分历史或插件状态未共享的问题。

临时替换共享 `auth.json` 也被排除，因为并发窗口可能在替换期间读取错误 token。

## 运行时设计

### 配置识别

`cc_switch_config.py` 使用标准库 `tomllib` 解析 `settings_config.config`：

1. `model_provider` 必须是非空字符串。
2. `model_providers[model_provider]` 必须是对象。
3. `auth` 必须只包含一个字符串键 `OPENAI_API_KEY`，值继续沿用现有环境安全校验。
4. 该 provider 必须使用当前 CC Switch 格式：`requires_openai_auth = true` 且没有
   `env_key`。

不满足以上结构时抛出脱敏后的 `CcSwitchConfigError`，说明交互启动器只支持单一 API-key
中转站。验证在临时 profile 写入前完成。

### Runtime 契约

`CodexProfileRuntime` 新增：

```python
config_overrides: tuple[str, ...]
```

该字段包含两个完整的 `key=value` 覆盖字符串，不含认证值。provider ID 使用 TOML 引号键
编码，避免点号、空格或其他合法名称改变 dotted-key 结构。

现有 `environment` 继续从父环境复制，再由所选 provider 的 `OPENAI_API_KEY` 覆盖。父进程
环境不变；`CODEX_INTERNAL_ORIGINATOR_OVERRIDE` 的默认与显式覆盖行为不变。

### 启动命令

`codex_tui.py` 构造：

```text
codex
  --profile <random-profile>
  -c <requires_openai_auth override>
  -c <env_key override>
  <forwarded Codex arguments>
```

生成的覆盖位于用户转发参数之前。用户的 `resume`、session ID、`--model`、`-c/--config`
继续保持原顺序；原有 `--profile/-p` 冲突检查不变。

## 错误与安全

- TOML 解析失败、活动 provider 缺失、provider 表缺失或认证形状不支持时，在启动 Codex 前
  返回退出码 `2`。
- 公共错误不包含配置正文、认证值或底层解析异常链。
- token 不写入临时 profile；profile 仍为随机名称、排他创建、权限 `0600`。
- token 不进入 `config_overrides`、子进程参数、`repr` 或 README 示例。
- 正常退出、body 异常、Ctrl-C 和 SIGTERM 的 profile 清理语义不变。
- 并发启动各自使用独立环境和 profile，共享 sessions 与 SQLite，但不共享所选 token。

## 测试

使用标准库 `unittest` 和 mock，不启动真实 TUI、不发送模型请求。

配置运行时回归测试：

- 模拟共享 `auth.json` 为 provider A、所选 provider B 的环境 key 为另一值。
- runtime 环境必须包含 B 的 key，父环境及共享 `auth.json` 必须仍为 A。
- runtime 必须生成针对活动 provider ID 的两个配置覆盖。
- `custom` 与 `hlool` 等不同 provider ID 均正确编码。
- TOML 无效、缺少活动 provider、复杂 auth 和 `OpenAI Official` 形状在写 profile 前被拒绝。
- 覆盖字符串、异常和 `repr` 不包含 key 值。

启动器回归测试：

- `subprocess.run` 命令在 `--profile` 后、转发参数前包含两个 `-c` 覆盖。
- `resume SESSION_ID --model ...` 的转发顺序不变。
- 命令列表不包含认证值；环境包含所选认证值。
- 无额外参数、子进程退出码、可执行文件错误和清理错误行为保持不变。

最终验证：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_tui.py codex_candy_eval.py \
  codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
```

手动验收由用户在 App 选择非 wuming provider 时运行：

```bash
python3 ~/to7for/nice/codex-candy-eval/codex_tui.py \
  --cc-switch-config wuming
```

预期请求使用 wuming 的地址与 key，已有窗口和 App 选择不变；退出后共享 sessions 保留，临时
profile 被删除。
