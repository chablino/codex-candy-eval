# Codex 默认插件开关设计

日期：2026-08-05
状态：已完成方案确认，等待书面规格审阅

## 背景

当前 launcher 将共享插件缓存视为全局安装状态，但对 provider、已启用的 Common Config 和
provider sidecar 都没有声明的插件默认写入 `enabled = false`。这保证了 provider 隔离，却也
意味着每个新 provider 都要在 `/plugins` 中重新开启同一组常用插件。

用户希望增加一份独立、稳定的默认插件开关配置。它以 anyrouter 当前配置为一次性快照，不
持续跟随 anyrouter，也不依赖 CC Switch App 当前选择。默认层必须可被当前 provider、按条件
合并的 Common Config，以及当前 provider 的 `/plugins` sidecar 覆盖。

## 目标

1. 所有通过 `codex_tui.py` 启动的 provider 默认启用 anyrouter 当前配置中的全部 8 个插件。
2. 默认值是项目内固定快照，不读取或追踪 anyrouter 后续变化。
3. provider、Common Config 和 provider sidecar 仍能按既有规则覆盖默认值。
4. 默认值只对共享 inventory 中当前确实已安装的插件生效。
5. 全局卸载默认插件后不自动下载、不生成缺失警告；重新安装后默认值重新生效。
6. 不修改 CC Switch SQLite 数据库，也不改变 `Apply Common Config` 的条件合并语义。

## 非目标

- 不增加“从 anyrouter 刷新默认值”的命令。
- 不让默认配置持续跟随 anyrouter 或 CC Switch 当前选择。
- 不修改 `claude_tui.py` 或 Claude Code。
- 不自动迁移、删除或重写已有 provider sidecar。
- 不自动安装、重新安装或卸载任何插件。
- 不把默认插件设置存入 `~/.codex/.cc-switch-tui/`。

## 默认配置文件

项目根目录新增 `codex_plugin_defaults.toml`。launcher 使用
`Path(__file__).with_name("codex_plugin_defaults.toml")` 定位文件，因此从其他工作目录调用
脚本时仍读取同一份配置。

文件固定包含 anyrouter 当前启用的全部 8 个插件：

```toml
[plugins."superpowers@openai-api-curated"]
enabled = true

[plugins."documents@openai-primary-runtime"]
enabled = true

[plugins."pdf@openai-primary-runtime"]
enabled = true

[plugins."presentations@openai-primary-runtime"]
enabled = true

[plugins."template-creator@openai-primary-runtime"]
enabled = true

[plugins."spreadsheets@openai-primary-runtime"]
enabled = true

[plugins."visualize@openai-bundled"]
enabled = true

[plugins."browser@openai-bundled"]
enabled = true
```

虽然 `/plugins` 当前界面只把其中 6 个计入可见 Installed 列表，`browser` 和 `visualize` 也
属于 anyrouter 当前启用状态，因此保留在默认快照中。

默认文件的 schema 有意保持狭窄：

- 顶层必须恰好只有 `plugins` table；
- 每个 key 必须是合法的字符串插件 ID；
- 每个插件值必须是 table，且必须恰好只有布尔 `enabled`；
- Superpowers 的旧 ID 仍按现有规则归一化为
  `superpowers@openai-api-curated`；
- 同时声明新旧 Superpowers ID 时，canonical ID 的显式值优先。

文件缺失、符号链接、不是普通文件、TOML 损坏、未知字段或类型错误时，launcher 在创建 Codex
子进程前以固定、脱敏的配置错误失败。错误不包含文件内容或 provider 密钥。

## 配置合成

每次启动先读取并校验默认插件文件，再扫描共享安装 inventory。只有同时满足以下条件的默认
条目才进入本次基础配置：

1. 默认文件声明了该插件；
2. 归一化后的插件 ID 存在于当前共享 inventory。

完整优先级从低到高为：

```text
当前已安装的默认插件
→ 当前 provider 原始配置
→ commonConfigEnabled=true 时的 Common Config
→ 当前 provider 的 /plugins sidecar
```

默认层只提供 `enabled`，provider 或 Common Config 中同一插件的其他字段仍按现有结构化深度
合并规则保留。provider/Common 显式 `enabled = false` 会关闭默认开启的插件；sidecar 的
布尔值最后覆盖所有配置声明。

未在默认文件、provider、Common Config 或 sidecar 中声明，但存在于 inventory 的插件继续
注入 `enabled = false`。provider 或 Common Config 显式启用但 inventory 缺失的插件仍沿用
现有警告；只有默认文件中的缺失插件会被静默过滤，因为默认层不得触发重装提示。

## Baseline 与 Sidecar

`ComposedConfig.baseline_plugins` 改为“已安装默认值 + provider + 可选 Common Config”的完整
有效插件基线，不包含 provider sidecar。

因此：

- 默认启用的插件在某个 provider 的 `/plugins` 中关闭后，该 provider sidecar 保存
  `false`；
- 再次开启并回到默认 `true` 时，该稀疏 sidecar 项可以删除；
- provider/Common 显式覆盖默认值时，回到该显式值同样会删除冗余 sidecar；
- 已有 sidecar 不做主动迁移，启动时继续以最高优先级生效。

现有 sidecar 中与新默认值相同的冗余 `true` 不影响行为。只有用户后续实际改变相应插件，或
使用既有 `--reset-plugin-state`，才按当前基线自然收敛；launcher 不在启动时批量重写状态。

## 安装与卸载

默认文件不是安装清单，inventory 仍是全局安装事实来源：

- 默认插件已安装：对没有更高优先级覆盖的 provider 启用；
- 默认插件被全局卸载：下一次启动时从默认层过滤，不自动下载、不警告缺失；
- 默认插件后来重新安装：下一次启动时重新进入默认层；
- 非默认插件安装：仍只为执行安装的当前 provider 记录启用，其他 provider 默认禁用；
- 任意插件全局卸载：仍按现有逻辑清除所有 provider sidecar 中对应记录。

## 组件边界

### `codex_runtime.py`

- 定位、读取并校验项目默认 TOML；
- 将默认插件布尔映射传给有效配置合成；
- 将文件 I/O 与解析错误转换为脱敏 `CcSwitchConfigError`；
- 不读取 anyrouter provider，不新增 CC Switch 查询。

### `codex_plugin_state.py`

- 接受已校验的默认插件映射；
- 规范化默认 ID，并按 inventory 过滤；
- 按既定优先级生成插件表和 baseline；
- 保持 sidecar、并发同步和全局删除行为不变。

### `codex_plugin_defaults.toml`

- 只保存 8 个稳定的默认布尔开关；
- 不包含 provider、模型、marketplace、认证或 Common Config。

## 测试与验收

实现采用 TDD，至少覆盖：

1. 默认文件解析得到恰好 8 个 canonical 插件 ID，且全部为 `true`。
2. 从不同当前工作目录启动仍读取脚本旁的默认文件。
3. 缺失、符号链接、非普通文件、损坏 TOML、额外顶层字段、额外插件字段和非布尔值均脱敏
   失败。
4. 默认值只应用于 inventory 中已安装的插件；缺失默认插件不产生 warning。
5. 覆盖顺序严格为默认值、provider、Common Config、sidecar。
6. baseline 包含已安装默认值；关闭默认插件保存当前 provider 的 `false`，恢复默认值删除
   稀疏覆盖。
7. 全局卸载默认插件后不进入临时配置；重新出现于 inventory 后再次默认启用。
8. 非默认插件仍保持“安装全局、只启用当前 provider”。
9. 既有插件并发、reset、Common Config 条件和 CC Switch 数据库只读测试无回归。
10. 使用真实 Codex CLI 新启动一个没有插件声明和 sidecar 覆盖的 provider，确认 8 个默认
    插件均为 enabled；CC Switch 数据库前后字节或哈希保持一致。

手工验收以新启动的 launcher 会话为准。已经运行的 Codex 进程保留启动时配置，不热更新。
