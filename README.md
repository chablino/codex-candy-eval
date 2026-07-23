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

配置在进程启动时读取一次，并写入权限为 owner-only 的临时目录：Codex 使用临时
`CODEX_HOME`，Claude Code 使用临时 `CLAUDE_CONFIG_DIR`。评测期间不会调用 `cc-switch`
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
