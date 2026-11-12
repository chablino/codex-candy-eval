# Codex 降智测试

用本地 Codex CLI 批量测试一道糖果数学题，并统计 reasoning tokens 与正确率。

![example](./example.png)

## 用法

该脚本无任何第三方依赖，只需要您已安装并登录 [Codex CLI](https://github.com/openai/codex)

```bash
python codex_candy_eval.py -m gpt-5.5 -r high -n 5
```

四个评测脚本都可以在不切换 CC Switch App 当前配置的情况下，指定一个配置运行：

```bash
python codex_candy_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high -n 5
python codex_tps_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high
python codex_juice_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol
python claude_candy_eval.py --cc-switch-config anyrouter -m sonnet -r high -n 5
```

`--cc-switch-config` 接受 CC Switch App 中的显示名称或 provider ID，默认从
`~/.cc-switch/cc-switch.db` 只读查找。精确 ID 优先；同类型配置名称重复时，请使用 ID。
脚本会自动限定配置类型：三个 Codex 脚本只查找 `app_type=codex`，Claude candy 只查找
`app_type=claude`，所以两种配置使用同一个名称也不会混用。

### 临时启动其他 Codex 配置

如果 CC Switch App 当前选择了配置 A，可以在不切换 App、不影响配置 A 已有窗口的情况下，
用另一个 Codex provider 启动交互式 TUI：

```bash
python3 codex_tui.py --cc-switch-config jianzhile
```

`--` 后面的参数会按原顺序传给 Codex。例如在当前目录打开会话恢复列表：

```bash
python3 codex_tui.py --cc-switch-config jianzhile -- resume
```

也可以在任意目录按 session ID 恢复，并为这次启动指定模型：

```bash
python3 /path/to/codex-candy-eval/codex_tui.py \
  --cc-switch-config jianzhile -- \
  resume SESSION_ID --model gpt-5.6-sol
```

启动器只查找 `app_type=codex` 的 provider，并从 CC Switch SQLite 数据库只读加载指定
provider；普通启动不会读取或改变 App 当前选中的 provider，也不会修改数据库。下面介绍的
显式 `mcp add/remove` 命令会备份并保存所选 provider 的 MCP 声明。启动时读取共享
`CODEX_HOME/config.toml`（默认 `~/.codex/config.toml`）的用户设置，再使用所选 provider
的地址、凭证、模型、推理档位和兼容参数。全局文件中当前另一家 provider 的路由、凭证、
模型及默认 profile 不会带入。插件、权限、功能开关、TUI 和全局 MCP 等来自当前共享文件；
数据库中旧 `common_config_codex` 和 `commonConfigEnabled` 已不再读取，供应商行中遗留的
插件、权限等旧全局字段也不再参与启动。

项目 `codex_plugin_defaults.toml` 只提供已安装插件的兜底开关；共享 `config.toml` 的显式
插件设置覆盖它，最后叠加该 provider 保存的 `/plugins` 开关。每次启动重新读取共享文件。

每次启动都会创建独立的临时 `CODEX_HOME`，其中包含这次运行的完整 `config.toml`，不使用
Codex profile。provider 的 token 通过该子进程的 `OPENAI_API_KEY` 注入，并写入本次临时
home 中权限为 `0600` 的 `auth.json`；临时 `config.toml` 只保存环境变量名称。这个认证文件
让 launcher 与正常直接运行的 Codex 使用相同的 API-key curated marketplace。共享
`CODEX_HOME`（未设置时为 `~/.codex`）中的默认 `config.toml`、`auth.json` 和 CC Switch
配置在普通启动时都不会被覆盖；正常退出、异常、Ctrl-C 或 SIGTERM 后，插件状态同步完成再删除整个临时
目录及其中的认证文件。

不同 provider 仍共享 sessions、SQLite 状态、history、skills、rules、memories 和插件目录，
所以可以按 session ID 跨 provider 使用 `resume`。每个并发窗口都有独立临时配置；插件开关
在退出时按实际变化加锁合并，不会因为另一个较早启动的窗口退出而整份覆盖。

Codex 0.147.0 起，SQLite thread 索引会保存 rollout JSONL 的绝对路径。启动器仍使用临时
`CODEX_HOME` 隔离配置。`sessions` 和 `archived_sessions` 会在临时 home 中使用真实目录，
已有文件以硬链接复用；这样活动 TUI 的 `/fork` 看到的 rollout 路径仍位于当前 Codex home，
不会触发新版 app-server 的路径校验。启动 `resume`/`fork` picker 前，启动器还会把当前
`model_provider` 的历史 rollout 映射到本次临时 home；直接传 session ID 时只映射该记录。
退出时新文件同步回共享目录，并只把属于本窗口临时 home 的 rollout 路径校验并改回共享
`CODEX_HOME/sessions/...`。启动时只修复临时 home 已经消失、且共享 JSONL 仍存在的失效
记录；其他窗口的临时 home 仍存在时会保留其路径，避免一个窗口启动或退出后，另一个
窗口的分叉操作报 `rollout path must be in Codex home directory`。新会话也不会在临时目录
删除后变成只能看到但无法 fork 的记录。共享配置中的 `sqlite_home`
不会覆盖启动器选定的共享 SQLite 目录。

Codex 0.156.1 的 rollout 文件还可能使用 `...-SESSION_ID_SEGMENT_UUID.jsonl` 格式。
启动器同时支持这种带分段 UUID 的文件名和旧格式，并校验文件首行的会话 ID。旧启动器
曾跳过新格式文件的路径修复，导致 Ctrl+C 退出后，虽然共享 JSONL 已保存，`resume`
仍报 `no rollout found`。更新后，下次启动会自动修复这类指向已删除临时目录的索引。

新版 Codex 的默认 `resume`/`fork` picker 还会按当前 `model_provider` 展示会话；`--all` 只取消
当前工作目录过滤。比如 `hlool` 的 provider ID 是 `hlool`，而 `muyuan` 的 provider ID 是
`custom`，即使两者使用同一个文件夹和同一份 SQLite，picker 内容也可能明显不同。这不代表
sessions 被隔离。跨 provider 继续或分叉已知会话时，请直接传 session ID：

```bash
python3 codex_tui.py --cc-switch-config muyuan -- resume SESSION_ID
python3 codex_tui.py --cc-switch-config muyuan -- fork SESSION_ID
```

刚启动且尚未提交过任何一轮消息的空白 TUI 还没有持久化 rollout；此时分叉没有历史可复制，
Codex 0.147.0 可能提示 `no rollout found`。先提交至少一轮消息再 `/fork`，或者直接再启动一个
空白窗口即可；这与上述旧临时路径迁移是两个不同情况。

插件的安装和删除是全局的，启用和关闭是按 provider 保存的：

- 在某个 provider 的 `/plugins` 中安装插件，会把插件安装到共享插件目录，并只为当前
  provider 启用；其他 provider 能看到这个已安装插件。若它不在项目默认文件中，其他
  provider 仍默认不会自动启用。
- 在 `/plugins` 中关闭或重新开启插件，只更新当前 provider 的开关。开关保存在共享
  `CODEX_HOME/.cc-switch-tui/provider-plugins/`，不写入 CC Switch 数据库。
- 在 `/plugins` 中删除插件，会从共享插件目录全局删除，并清除所有 provider 对它保存的
  开启覆盖；明确保存的关闭覆盖仍保留。如果该插件在当前 provider 的配置或项目默认值中
  默认开启，卸载会为当前 provider 保存 `false`，避免下次启动或其他 provider 重新安装后
  又自动启用。
- 共享 `config.toml` 声明插件默认开关；当前 provider 后续在 `/plugins` 中做出的开关
  变化优先于这个默认值。

`codex_plugin_defaults.toml` 当前与普通 `codex` 的全局配置保持一致，默认启用 Documents、
PDF、Presentations、Spreadsheets 和 Template Creator。Superpowers、HyperFrames、Browser、
Visualize 等插件不在默认表中，仍然需要按 provider 在 `/plugins` 中主动启用。默认文件不是
安装清单：只有共享 inventory 中已安装、且被某层明确启用的条目才进入临时配置。安装一个
插件或编辑另一个中转站不会改变 provider 的 sidecar；修改默认表后，应重新验证各 provider
的关闭覆盖。

插件开关的完整覆盖顺序从低到高为：已安装的项目默认值、共享 `config.toml`、当前 provider
的 `/plugins` sidecar。前两层构成 baseline，因此关闭默认插件只会为当前 provider 保存
一个稀疏的 `false`；恢复到 baseline 后，这条覆盖会自动删除。

从全局文件删除插件条目表示不再声明该值，不等于禁用：如果项目默认列表仍启用它，下次
启动会继承这个默认值。要修改所有 provider 的默认值，请在共享 `config.toml` 设置：

```toml
[plugins."superpowers@openai-api-curated"]
enabled = false
```

已有 `/plugins` 覆盖的优先级高于共享配置；开关在启动器退出时才保存，未退出的
窗口中的操作尚不会作用于新窗口。旧版启动器卸载插件时会清除关闭记录，更新代码无法恢复
已经丢失的选择，需要重新关闭一次。直接从 Codex 桌面应用或普通 `codex` 启动的会话操作
插件，不会写入本启动器的 provider 开关。

当前 `superpowers` 的 canonical ID 是 `superpowers@openai-api-curated`。
`superpowers@openai-curated` 是旧 marketplace ID，启动器会将旧配置声明归一化为 canonical
ID。`openai-curated` 完整目录的缓存不会被当作 launcher 的已安装 inventory，避免把其中的
Figma、GitHub、HyperFrames 或另一份 Superpowers 错误注入当前 provider 配置。

如果只想清除某个 provider 通过启动器保存的插件开关，让它重新采用当前共享默认值，
可以使用：

```bash
python3 codex_tui.py --cc-switch-config fengwind --reset-plugin-state
```

该选项不会卸载全局插件，也不会影响其他 provider。交互启动器目前只支持认证对象中仅含一个
`OPENAI_API_KEY` 的 CC Switch Codex 中转站配置，不支持 `OpenAI Official` 等包含登录
tokens 的复杂认证配置；不支持的配置会在启动 Codex 前安全退出。启动器提供完整临时配置，
所以不允许在转发参数中再次使用 `--profile` 或 `-p`；`--model` 和 `-c/--config` 等其他
Codex 参数仍可正常传递。

### MCP：共享配置与 provider 专属配置

所有 provider 共用的 MCP，请直接在 CC Switch 或 `~/.codex/config.toml` 中管理，启动器
会在下次启动时读取。只有某个 provider 需要独立声明时，才使用下面的命令。

直接编辑某个 provider 的专属 MCP 配置：

```bash
python3 codex_tui.py --cc-switch-config lanln --edit-mcp
```

它只显示这份 provider 的 MCP 内容。要添加 dbx，加入下面这一段；不需要时，重新运行
同一个命令并删掉这一段。保存并退出编辑器后永久保存，下次启动 `lanln` 时生效：

```toml
[mcp_servers.dbx]
command = "/Users/chablino/.dbx/bin/dbx-mcp"
```

编辑器依次取 `VISUAL`、`EDITOR`，未设置时使用 `nano` 或 `vi`。默认 `nano` 中按
`Ctrl+O`、回车保存，再按 `Ctrl+X` 退出；整个文件留空会删除这份 provider 自己的所有 MCP。
只修改 MCP，模型、凭证等配置不在这个编辑入口中。无修改直接退出不会保存或生成备份。

也支持用 Codex 原生 MCP 命令添加或删除。例如只给 `lanln` 添加 dbx：

```bash
python3 codex_tui.py --cc-switch-config lanln -- \
  mcp add dbx -- /Users/chablino/.dbx/bin/dbx-mcp
```

以后照常启动即可，无需重复添加：

```bash
python3 codex_tui.py --cc-switch-config lanln
```

不想让 `lanln` 使用时，只从 `lanln` 删除，不影响其他 provider：

```bash
python3 codex_tui.py --cc-switch-config lanln -- mcp remove dbx
```

将 `lanln` 换成其他 provider 的显示名称或 ID，就能管理那份配置。原生 `mcp add` 的
`--env`、`--url` 等参数也可以照常使用；命令请使用上面的 `-- mcp add ...` 或
`-- mcp remove ...` 顺序。原生命令成功后，启动器会显示实际保存的 provider 和备份路径。
Codex 原生输出中的 “global MCP” 指它本次操作的临时 home，最终由启动器保存到指定
provider，不会修改真实的 `~/.codex/config.toml`。

保存位置就是 CC Switch 数据库中该 provider 原有的 `settings_config.config.mcp_servers`，
不会增加 MCP sidecar。每次实际修改前，旧记录会备份到
`~/.cc-switch/backups/codex-tui-mcp/`，目录权限为 `0700`，备份文件为 `0600`。
保存只合并变化的 MCP 条目，保留凭证、模型、插件及其他 provider 的数据；同一 MCP 被
同时修改时会拒绝覆盖并提示重试。普通 TUI 启动、`mcp list/get` 和临时 `-c` 参数不会写入数据库。

查看启动时实际生效的 MCP：

```bash
python3 codex_tui.py --cc-switch-config lanln -- mcp list
```

已启动窗口使用启动时的配置，添加或删除后需要重启该窗口；进入后可用 `/mcp verbose`
查看连接状态和工具。资源列表为空不代表 MCP 未连接：MCP 可以只提供工具。

所有 provider 共用的 MCP 直接在 CC Switch MCP 页面或共享 `~/.codex/config.toml` 中管理，
也可以直接运行 `codex mcp add/remove`。启动器下次启动会自动读取，无需 `--edit-mcp`。

`--edit-mcp` 和上述 `-- mcp add/remove` 仅用于 provider 专属声明。启动时先读取全局 MCP，
再叠加 provider 的 MCP；同名服务器整项由 provider 覆盖，避免混用另一份配置的凭证。
删除 provider 声明会重新继承同名全局服务器，不会删除全局 MCP。若要只对某个 provider
禁用全局服务器，可在其专属声明中设置 `enabled = false`。

### HyperFrames：统一从插件入口使用

`/plugins` 只控制插件提供的技能。独立安装在 `~/.agents/skills/` 或 `~/.codex/skills/`
中的同名技能有自己的加载入口，关闭插件不会关闭这些独立副本。HyperFrames 的某些
`init` 和 `skills update` 版本会把技能安装到全局目录，因此可能出现插件已关闭、动画任务
仍自动使用 HyperFrames 的情况。

本机整理后，HyperFrames 的 11 个独立技能已归档到
`~/.codex/skill-backups/hyperframes-日期时间/`。日常在对应中转站启动的 `/plugins` 中
启用或关闭 HyperFrames，并正常退出以保存选择。保留的官方插件当前提供 5 个技能；
归档中的新版独立技能与它不是同一版本。插件更新通过插件管理器进行。

启动器可读取共享 `CODEX_HOME/.cc-switch-tui/local-skills.toml` 中的本机策略。例如：

```toml
disabled_skills = [
  "/absolute/path/to/.agents/skills/hyperframes/SKILL.md",
]
skip_hyperframes_auto_install = true
```

本机策略使用需要禁用的独立 `SKILL.md` 的绝对路径，并在每次生成临时配置时应用，
因此归档后的文件即使被重新安装到原路径，也保持禁用。策略只作用于列出的路径，
插件缓存内的技能和插件开关仍正常工作；其他未列出的个人技能不受影响。
`skip_hyperframes_auto_install` 同时向 Codex 进程及其工具命令设置
`HYPERFRAMES_SKIP_SKILLS=1`，用于跳过当前 HyperFrames CLI 的自动技能安装。
它不拦截用户明确运行的安装命令，也不保证旧 CLI 支持此变量；路径禁用规则负责避免
独立副本再次被加载。策略文件可选，缺失时维持原有行为，不改变其他机器的默认配置。

App 和普通 `codex` 不读取启动器策略，所以本机的 `~/.codex/config.toml` 也单独保存了
相同路径的 `[[skills.config]] enabled = false` 和工具环境设置。如果以后通过其他配置
工具替换这个文件，需要保留这些条目。启动器不会把共享配置中的其他插件或技能开关
整体继承到各中转站。

备份目录中的 `RESTORE.md` 记录原位置、归档清单和恢复步骤；`restore.py` 默认仅预览，
加 `--apply` 才恢复。恢复脚本遇到后来修改过的配置或已存在的目标目录时会停止，避免
覆盖后续设置。安装登记仍保留，供 Claude Code、Cursor 中未改动的技能副本继续使用。
整理或恢复后应启动新会话；旧会话已经读入的技能指令不会自动撤回。

### 临时启动其他 Claude Code 配置

Claude Code 也可以在不切换 CC Switch App 当前配置的情况下，临时使用另一个 Claude
provider 启动交互窗口：

```bash
python3 claude_tui.py --cc-switch-config anyrouter
```

继续当前目录最近的会话，或在任意目录按 session ID 恢复：

```bash
python3 claude_tui.py --cc-switch-config anyrouter -- --continue
python3 /path/to/codex-candy-eval/claude_tui.py \
  --cc-switch-config anyrouter -- --resume SESSION_ID
```

启动器和 `claude_candy_eval.py` 优先运行 PATH 中的 `cometix`
（[@cometix/claude-code](https://github.com/CometixSpace/claude-code)，Claude Code 的 Node.js
版本），找不到时才回退到官方 `claude`。cometix 的 npm 包命令名同样是 `claude`，与官方版本
共存时，可以装到独立目录再链接成 `cometix`：

```bash
npm install --prefix ~/.local/share/cometix-claude @cometix/claude-code
ln -s ~/.local/share/cometix-claude/node_modules/.bin/claude ~/.local/bin/cometix
```

cometix 需要 Node.js 24 或更新版本，与官方版本使用同一个 `CLAUDE_CONFIG_DIR`（默认
`~/.claude`）。

Claude 启动器和 selector 评测每次启动读取共享 `CLAUDE_CONFIG_DIR/settings.json`
（默认 `~/.claude/settings.json`）。插件、hooks、权限、statusLine 和普通环境变量等全局
设置以这个文件为准；地址、凭证、模型和供应商兼容参数来自所选 provider。当前全局文件
中的另一家 provider 的连接字段不会带入。数据库里的旧 `common_config_claude` 和
`commonConfigEnabled` 已不再使用，供应商行中遗留的插件、hooks 等全局字段也不会恢复。

共用的 `DISABLE_AUTOUPDATER`、`CLAUDE_CODE_EFFORT_LEVEL`，以及 cometix 的环境变量补丁
（如 `CLAUDE_CLASSIFIER_MODEL`、`CLAUDE_CODE_CONTEXT_LIMIT`），请在共享 `settings.json`
的 `env` 中设置，或在 shell 中导出。合并后的 `env` 会注入 Claude 子进程，启动时读取
环境变量的补丁也能生效。文件不存在时使用空的共享设置；文件损坏或无法读取会报错，
不会回退到数据库中的旧片段。

Claude Code 还把这个环境变量当作整个会话的强制值，设置后 `/effort` 只会提示
`CLAUDE_CODE_EFFORT_LEVEL=max overrides this session`，无法切换。因此启动器会把上述来源中
取值为 `low`、`medium`、`high`、`xhigh` 或 `max` 的这一项从 `env` 中移除，改为启动参数
`--effort`。它只决定会话的初始强度，进入后仍可以用 `/effort` 随时切换，与 Codex 中
`model_reasoning_effort` 作为默认值的用法一致。共享文件中的 `effortLevel` 和 `modelSettings` 也会随用户设置载入；需要显式指定启动
强度时，可设置 `CLAUDE_CODE_EFFORT_LEVEL`。转发参数中显式传入的
`--effort` 优先；`auto`、`unset` 等 `--effort` 不接受的取值保持原样留在 `env` 中。在 shell
中导出的 `CLAUDE_CODE_EFFORT_LEVEL` 不会被改动，仍会锁定整个会话。selector 评测也按同样
规则处理：未传 `-r` 时使用这个值，传了 `-r` 则以 `-r` 为准，不会再被它悄悄覆盖。

启动器只查找 `app_type=claude` 的 provider。每个窗口都有自己的临时 `CLAUDE_CONFIG_DIR`。
除启动器生成的 settings 外，其中所有条目都以符号链接指向共享 Claude 目录（父进程的
`CLAUDE_CONFIG_DIR`，未设置时为 `~/.claude`）；`.claude.json` 指向 Claude Code 平时使用的
全局配置（未设置 `CLAUDE_CONFIG_DIR` 时为 `~/.claude.json`）。sessions、history、projects、
插件缓存、用户级 skills/agents/commands 和 `CLAUDE.md` 仍跨 provider 共享，所以配置 B
可以继续配置 A 创建的会话。CC Switch App 当前选择、已经运行的 Claude 窗口和共享
`settings.json` 都不会被修改。会话中新建、且共享目录里还没有的状态，会在退出时移回
共享目录。子进程设置了 `CLAUDE_SECURESTORAGE_CONFIG_DIR`，钥匙串中的凭证条目名与直接
运行 Claude Code 时相同。

共享设置复制进本窗口的 `settings.json`，插件开关再叠加该 provider 保存的选择。所选
provider 的连接配置写入 `provider-settings.json`，通过 `--settings` 加载。启动命令固定
使用 `--setting-sources user,project,local`，共享设置仍在 user 层，项目和 local settings
照常覆盖用户设置；更高优先级的 `--settings` 确保所选 provider 的地址、凭证、模型映射和
headers 生效。两份临时文件权限为 `0600`，目录为 `0700`；凭证不会进入命令行或公开错误。
正常退出、异常、Ctrl-C 或 SIGTERM 后清理临时目录，保留共享状态。

交互启动器支持包含非空 `ANTHROPIC_BASE_URL`，且仅包含
`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY` 二者之一的 CC Switch 中转站配置。
`Claude Official` 等依赖 OAuth/keychain 的配置不在支持范围内。启动器自身需要占用
`--settings`、`--setting-sources` 和 `--mcp-config`，所以不允许在转发参数中再次指定它们；
`--continue`、`--resume`、`--model`、`--effort` 和权限模式等其他 Claude 参数可正常传递。

provider 配置顶层的 `mcpServers`（JSON，与 `claude mcp add` 写入的格式相同）按
provider 隔离并在任意目录生效：启动器把它单独写入临时 `mcp.json`（`0600`），通过
`--mcp-config` 注入。Claude Code 会忽略 `--settings` 文件中的 `mcpServers`，所以不能
像其他设置一样走 settings 文件；`--mcp-config` 是可变参数，命令行中一律使用
`--mcp-config=<路径>` 的等号形式。已启动的窗口使用启动时的配置，增删 MCP 后需要重启
会话。注意 `claude mcp list` 只列持久化配置，不显示 `--mcp-config` 注入的 server；
会话内用 `/mcp` 查看连接状态。

所有 provider 共用的 Claude MCP 直接通过 CC Switch MCP 页面或 Claude 的全局 MCP
命令管理；共享 `.claude.json` 中的声明会由所有窗口读取，无需 `--edit-mcp`。
只有需要按 provider 保存独立 MCP 时，才使用启动器：

```bash
python3 claude_tui.py --cc-switch-config anyrouter --edit-mcp
```

编辑器里只有该 provider 的 MCP 服务器 JSON 对象：添加键以新增，删除键以移除，保存
退出后生效。编辑器依次取 `VISUAL`、`EDITOR`，未设置时使用 `nano` 或 `vi`。保存只合并
变化的 MCP 条目，保留凭证、模型、hooks 等其他配置；同一 MCP 被同时修改时会拒绝覆盖
并提示重试。每次实际修改前，旧记录备份到 `~/.cc-switch/backups/claude-tui-mcp/`，
目录权限为 `0700`，备份文件为 `0600`。

评测脚本通过 selector 运行时会在启动时读取一次配置，并写入权限为 owner-only 的临时
目录，共享设置与上述启动器使用相同来源：Codex 评测使用临时 `CODEX_HOME`，Claude candy 评测使用临时
`CLAUDE_CONFIG_DIR`。评测期间不会调用 `cc-switch`
CLI，不会切换或写入 App 当前配置，也不会修改默认的 `~/.codex` 或 `~/.claude`；进程结束、
异常、Ctrl-C 或收到 SIGTERM 后会清理临时目录。

selector 模式目前只支持 macOS/Linux。原有不带 selector 的命令在 Windows 和
`curl/wget | python` 单文件模式下保持可用；管道模式若要指定配置，请先克隆完整仓库，
因为共享的只读配置模块不随单个脚本下载。

### 按 provider 开关 Claude 插件

在启动器窗口中用 `/plugin` 启用或禁用插件，再运行 `/reload-plugins`，即可当场生效。
正常退出时，这些变化只为当前 provider 保存，下次启动该 provider 时继续生效，其他 provider
不受影响。共享 `settings.json` 提供所有 provider 的插件默认值。

插件开关的优先级从低到高为：共享 `settings.json` 的 `enabledPlugins`、该 provider
通过 `/plugin` 保存的开关。保存位置是共享 Claude 目录下的
`.cc-switch-tui/provider-plugins/`，只记录与共享默认值不同的项，改回默认值后自动删除。
不写 CC Switch 数据库或共享 `settings.json`。多个窗口退出时只合并各自实际改动的插件；
开关在退出时保存，对已经运行的其他窗口不生效。

要清除某个 provider 保存的插件开关、回到当前共享默认值：

```bash
python3 claude_tui.py --cc-switch-config ultrarouter --reset-plugin-state
```

插件的安装和卸载仍是全局的。卸载会清除所有 provider 保存的启用记录；如果当前 provider
的默认值是启用，还会为它保存一条禁用记录。每次启动时，启动器会检查共享安装记录，
只把仍已安装的插件开关写入窗口配置。已卸载插件留下的 `true`、`false` 或版本约束都
不会再写进去，避免自动重装或 cometix 的“failed to load”残留提示，无需手动清理。
共享默认值和 provider 的关闭偏好仍保留；以后在 `/plugin` 中主动重新安装时会重新适用。
仅在全局配置中写 `true` 不再代替安装操作。

已运行的其他窗口保留旧配置，卸载后应退出并重新启动。项目/local settings 或管理策略
中的独立插件声明仍由 Claude 读取，不在上述自动过滤范围内。若只想全局停用而不卸载，
请在共享 `settings.json` 中把插件的 `enabledPlugins` 值设为 `false`，并清除之前保存的
provider 启用覆盖。安装记录损坏或无法读取时，启动器会报错而不猜测插件状态。

窗口中修改的其他用户级设置（例如 `/theme`、`/statusline` 或 `/model` 的默认值）只对当前
窗口有效，退出后丢弃。用户级 skills、agents、commands 和 `CLAUDE.md` 与直接运行 Claude
Code 时一样加载。

Claude Code 修改 `.claude.json` 时使用的锁文件位于各窗口自己的临时目录，所以多个启动器
窗口之间、以及它们与直接运行的 Claude Code 之间，不再共用这把锁。几个窗口在同一瞬间写入
时，后写入者可能覆盖先写入者刚做的改动，例如项目信任状态或统计字段。

### 一键运行
以下任选其一
```bash
wget -qO- "https://raw.githubusercontent.com/haowang02/codex-candy-eval/main/codex_candy_eval.py" | python3 - -m gpt-5.5 -r high -n 5
```
```bash
curl -fsSL "https://raw.githubusercontent.com/haowang02/codex-candy-eval/main/codex_candy_eval.py" | python3 - -m gpt-5.5 -r high -n 5
```


参数：

- `-m, --model`：codex 模型名，省略则用本地默认
- `-r, --reasoning-effort`：`low/medium/high/xhigh`（默认 `medium`）
- `-n, --tests`：测试次数（默认 1）
- `--cc-switch-config`：指定 CC Switch 配置名称或 ID（可选）

正确答案为 **21**，脚本直接判断回答中是否出现独立的 `21`。

## 致谢

- [LINUX DO](https://linux.do/) - 新的理想型社区
