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

启动器只查找 `app_type=codex` 的 provider。它不会创建按 provider 隔离的
`CODEX_HOME`，而是继续使用当前 `CODEX_HOME`（未设置时为 `~/.codex`），因此所有
provider 共享 sessions、SQLite 状态、历史、skills 和 plugins：配置 B 可以在相同目录
通过 `resume` 找到配置 A 的会话，也可以在其他目录通过 session ID 恢复。

交互启动器目前只支持认证对象中仅含一个 `OPENAI_API_KEY` 的 CC Switch Codex
中转站配置，不支持 `OpenAI Official` 等包含登录 tokens 的复杂认证配置；不支持的配置会在
启动 Codex 前安全退出。启动器会在权限为 `0600` 的临时 profile 中让所选 provider 从
该子进程的 `OPENAI_API_KEY` 读取认证；profile 只保存环境变量名称，不保存 token。因此
即使 App 当前选择其他 provider，也不会误用共享 `auth.json` 中的 token。

每次启动只会在共享 `CODEX_HOME` 中创建一个权限为 `0600` 的随机临时 profile，provider
认证只注入该 Codex 子进程。退出、异常或收到 SIGTERM 后会删除该 profile，不修改 CC Switch
App 当前选择、父终端环境、默认 `config.toml` 或默认 `auth.json`。启动器自身需要占用
`--profile`，所以不允许在转发参数中再次使用 `--profile` 或 `-p`；`--model` 和
`-c/--config` 等其他 Codex 参数仍可正常传递。

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

启动器只查找 `app_type=claude` 的 provider，并继续使用当前 `CLAUDE_CONFIG_DIR`（未设置时
为 `~/.claude`），不会按 provider 隔离 sessions、history、projects、plugins 或 skills。
因此配置 B 可以继续配置 A 创建的会话，CC Switch App 当前选择、已经运行的 Claude 窗口和
共享 `settings.json` 都不会被修改。

CC Switch 所选 provider 的完整配置（包括顶层 `env`）会写入权限为 `0600` 的临时
settings 文件，同时同一份 `env` 会注入 Claude 子进程。启动命令固定使用
`--setting-sources project,local` 保留正常的项目和 local settings，并通过优先级更高的
`--settings` 确保指定 provider 的地址、凭证、模型映射和自定义 headers 最终生效。临时目录
权限为 `0700`，token、地址和 headers 不会进入命令行、日志或公开错误；临时 settings
仅在 Claude 进程运行期间存在，正常退出、异常、Ctrl-C 或 SIGTERM 后都会清理。

交互启动器支持包含非空 `ANTHROPIC_BASE_URL`，且仅包含
`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY` 二者之一的 CC Switch 中转站配置。
`Claude Official` 等依赖 OAuth/keychain 的配置不在支持范围内。启动器自身需要占用
`--settings` 和 `--setting-sources`，所以不允许在转发参数中再次指定它们；
`--continue`、`--resume`、`--model`、`--effort` 和权限模式等其他 Claude 参数可正常传递。

评测脚本通过 selector 运行时会在启动时读取一次配置，并写入权限为 owner-only 的临时
目录：Codex 评测使用临时 `CODEX_HOME`，Claude candy 评测使用临时
`CLAUDE_CONFIG_DIR`。评测期间不会调用 `cc-switch`
CLI，不会切换或写入 App 当前配置，也不会修改默认的 `~/.codex` 或 `~/.claude`；进程结束、
异常、Ctrl-C 或收到 SIGTERM 后会清理临时目录。

selector 模式目前只支持 macOS/Linux。原有不带 selector 的命令在 Windows 和
`curl/wget | python` 单文件模式下保持可用；管道模式若要指定配置，请先克隆完整仓库，
因为共享的只读配置模块不随单个脚本下载。

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
