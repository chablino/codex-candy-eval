# Codex 临时认证与插件目录一致性修复设计

日期：2026-08-05
状态：已完成方案确认，等待书面规格审阅

## 背景

`codex_tui.py` 为每次启动创建隔离的临时 `CODEX_HOME`，把所选 provider 的
`OPENAI_API_KEY` 放入子进程环境，并把当前 provider 转换成
`requires_openai_auth = false`、`env_key = "OPENAI_API_KEY"`。临时 home 没有
`auth.json`。

Codex 0.146.0 会据此选择不同的内置 marketplace 身份：

- 正常共享 home 有 API-key `auth.json` 时，目录名为 `openai-api-curated`，当前共
  29 个插件；加上 5 个 `openai-primary-runtime` 插件后，`/plugins` 显示 34 个。
- launcher 的临时 home 没有 `auth.json` 时，目录名为 `openai-curated`，当前共
  180 个插件；加上 5 个 primary-runtime 插件后，`/plugins` 显示 185 个。

现有 launcher 又把 Superpowers 统一写成
`superpowers@openai-api-curated`。因此临时运行时找不到该 ID，即使共享缓存中已经有
Superpowers，`/plugins` 仍会再次显示下载。缓存扫描还会把
`openai-curated` 下的 Figma、GitHub 和 HyperFrames 等缓存目录当作全局安装，向临时配置
注入 `enabled = false`，使它们出现在 Installed 列表中。

## 目标

1. 通过 launcher 启动与正常直接运行 `codex` 时使用相同的
   `openai-api-curated` marketplace 和插件 ID。
2. Superpowers 全局只使用 `superpowers@openai-api-curated`，下载一次后不再反复提示。
3. 不把 `openai-curated` 的缓存条目误判为 launcher 管理的已安装插件。
4. 保持既有语义：插件安装和卸载全局生效，启用和禁用只作用于当前 provider。
5. 不修改 CC Switch SQLite 数据库、provider 配置或 Common Config。
6. 不让认证信息进入持久 launcher 状态、日志或错误消息。

## 非目标

- 不修改 Claude Code 或 `claude_tui.py`。
- 不改变 Common Config 的条件合并规则。
- 不引入第二套 Superpowers ID 同步机制。
- 本次代码变更不自动删除用户目录中已有的
  `plugins/cache/openai-curated/superpowers`；验证完成后再单独征得用户同意进行精确清理。
- 不修改当前已经运行的 Codex 进程；修复只影响新启动的 launcher 会话。

## 方案

### 临时认证文件

launcher 继续把所选 provider 的 key 放入 `OPENAI_API_KEY` 环境变量，并继续把当前
model provider 转换为 `requires_openai_auth = false`。这保证模型请求仍使用所选 provider
自己的 `base_url` 和 key，而不是共享 home 中由 CC Switch 当前选项控制的认证。

在写入临时 `config.toml` 的同时，launcher 还在同一个临时 home 写入：

```json
{
  "OPENAI_API_KEY": "<selected-provider-key>"
}
```

文件名固定为 `auth.json`，权限为 `0600`，父目录权限继续为 `0700`。该文件不是符号链接，
也不连接或复制共享 home 的 `auth.json`。Codex 因而识别本次会话为 API-key 环境，选择
`openai-api-curated`，但模型请求仍由临时配置路由到所选 provider。

认证解析只保留一份经校验的 key 值，在临时配置准备成功后分别生成子进程环境和临时
`auth.json`。任何解析、序列化或写入错误都继续转换成固定的脱敏 launcher 错误。

### 生命周期

`auth.json` 与 `config.toml` 属于同一个 `TemporaryDirectory`。正常退出、Codex 非零退出、
Ctrl-C、SIGTERM、启动失败和退出同步失败都沿用既有 finalization 路径清理整个临时 home。
SIGKILL 仍无法执行进程内清理，这是现有临时目录模型的边界。

启动器不得把 key 写入：

- CC Switch 数据库；
- `~/.codex/.cc-switch-tui/` sidecar；
- 命令行参数；
- README、测试快照、日志或异常文本。

### 插件 inventory

launcher 的认证模式固定为 `openai-api-curated` 后，inventory 扫描忽略整个
`plugins/cache/openai-curated/` marketplace，而不再只忽略其中的 Superpowers。该目录是
另一种 marketplace 身份留下的缓存，不能代表本 launcher 模式下的全局安装状态。

`plugins/cache/openai-api-curated/`、`openai-primary-runtime/`、`openai-bundled/` 和用户明确
配置的其他 marketplace 仍按现有 manifest 校验规则扫描。这样既保留共享安装 inventory，
又不会把 Figma、GitHub、HyperFrames 等 `openai-curated` 缓存注入本次配置。

Superpowers 的规范 ID 保持为：

```text
superpowers@openai-api-curated
```

既有 sidecar 中的 `superpowers@openai-curated` 仍归一化到该 ID，以兼容当前版本已经写出的
状态文件。

## 失败处理

- provider `auth` 不是仅含字符串 `OPENAI_API_KEY` 的 object 时，在创建 Codex 子进程前失败。
- 临时 `auth.json` 无法以 owner-only 权限写入时，删除临时 home 并返回 launcher 错误码 `2`。
- marketplace 输出与预期不一致时，真实 CLI 集成测试失败；启动器运行时不猜测或自动改写
  marketplace。
- 共享缓存中同时存在两个 Superpowers 目录时，launcher 只承认
  `openai-api-curated`；旧目录保留到用户明确批准清理。

## 测试与验收

实现采用 TDD，覆盖：

1. 临时 runtime 创建真实、非符号链接、权限 `0600` 的 `auth.json`，内容只来自所选
   provider，并在 runtime 关闭后删除。
2. 模型 provider 仍为 `requires_openai_auth = false` 且通过 `env_key` 取 key，避免改变
   provider 路由。
3. 认证损坏和临时文件写入失败不会在异常中泄露 key。
4. inventory 忽略 `openai-curated` 下的全部插件，同时继续识别
   `openai-api-curated` 和其他 marketplace 的有效 manifest。
5. 使用真实 Codex CLI 对临时 runtime 执行 `plugin marketplace list --json`，确认存在
   `openai-api-curated`、不存在 `openai-curated`。
6. 使用真实 Codex CLI 执行插件列表，确认已经缓存并启用的
   `superpowers@openai-api-curated` 被识别为 installed，而不是 available/download。
7. 运行全部项目测试，确认 provider 隔离、Common Config、sidecar 并发和清理行为没有回归。

手工验收使用新启动的会话：

| 场景 | 期望结果 |
| --- | --- |
| 正常新终端运行 `codex` | `/plugins` 使用 `openai-api-curated`，总数为当前 API-key 目录数量加 5 |
| `codex_tui.py --cc-switch-config hlool` | 使用同一个 marketplace 身份，不再显示 185 项完整目录 |
| 已全局安装 Superpowers 后启动任一 provider | 不再要求重新下载；是否启用服从该 provider 的配置或 sidecar |
| 当前 provider 禁用 Superpowers | 只改变当前 provider；其他 provider 不受影响 |
| 存在 `openai-curated` 的 Figma 等缓存 | 不出现在 launcher 的 Installed/Disabled 列表中 |
