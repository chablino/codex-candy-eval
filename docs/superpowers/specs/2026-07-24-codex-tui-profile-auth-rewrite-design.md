# Codex TUI 临时 Profile 认证转换设计

日期：2026-07-24

状态：用户已复核，已实施

本设计取代
`docs/superpowers/specs/2026-07-24-cc-switch-codex-tui-api-key-auth-fix-design.md`
中的 CLI provider 子字段覆盖方案。共享 `CODEX_HOME`、子进程环境、会话共享、临时 profile
权限和清理设计保持不变。

## 问题与根因

为避免 Codex TUI 误用共享 `auth.json`，启动器加入了两条最高优先级 CLI 覆盖：

```text
model_providers."custom".requires_openai_auth=false
model_providers."custom".env_key="OPENAI_API_KEY"
```

随后 `muyuan` 在 Codex 0.145.0 启动阶段失败：

```text
model_providers."custom": provider name must not be empty
```

只读检查确认 CC Switch 中的 `muyuan`、`wuming` 和当前 `~/.codex/config.toml` 都包含
`name = "custom"`，所以原配置并不缺少名称，错误也不是 App 当前选择 `wuming` 导致的。

无真实 token、无模型请求的 `codex mcp list` 对照实验确认两个独立问题：

1. Codex CLI 的 `-c/--config` 键使用 dot notation，TOML 解析针对等号右侧的值。把
   TOML quoted-key 写法放进键路径后，Codex 0.145.0 不会把引号解释成路径编码，因而产生
   缺少 `name` 的错误 provider 条目。
2. 去掉引号只能在更低配置层已经存在同 ID 完整 provider 时成功。只有临时 profile 定义
   provider、基础配置没有同 ID 条目时，CLI 层的部分 provider 条目仍会遮盖 profile 中的
   完整条目并丢失 `name`。

因此，无论 quoted 还是 bare 子字段覆盖，都不能保证“App 当前任意配置 A，临时启动配置
B”。根因在于把一个必须保持完整的 provider 映射拆成了更高优先级的部分 CLI 配置。

## 目标

- App 当前配置与临时配置使用相同或不同内部 model provider ID 时都能正确启动。
- 所选配置必须从其子进程环境中的 `OPENAI_API_KEY` 读取认证，不读取共享 `auth.json`。
- 保留所选 CC Switch profile 的所有顶层设置，例如 model、reasoning、projects、plugins、
  marketplaces 和 TUI 配置。
- token 只存在于子进程环境，不进入 argv、临时 profile、异常、日志或对象 `repr`。
- 不修改共享 `auth.json`、默认 `config.toml`、CC Switch 数据库、父环境或已运行窗口。
- 继续共享 sessions、SQLite、历史、skills 和 plugins。
- 保持 `resume`、session ID、`--model` 以及用户自带 `-c/--config` 参数顺序。

## 方案比较

### 方案 1：转换临时 profile 并验证语义等价（采用）

只改变 owner-only 临时 profile 内活动 provider 的两个认证字段：

```toml
requires_openai_auth = false
env_key = "OPENAI_API_KEY"
```

转换后再次使用 `tomllib` 解析，并要求解析结果等于“原解析树深拷贝后仅修改上述两个字段”
的期望树。任何无法唯一、等价转换的格式都在写文件前拒绝。

该方案不新增 provider CLI 覆盖，因此不会发生跨配置层的 provider 条目遮盖；URL 和其他
provider 设置也不会进入 argv。

### 方案 2：使用 bare CLI 子字段路径（不采用）

`model_providers.custom...` 能修复当前 `wuming(custom) → muyuan(custom)`，但当基础配置
不存在相同 provider ID 时仍会产生不完整条目。它只能修复一个配置组合，不满足原始目标。

### 方案 3：在 CLI 中重建完整 provider（不采用）

本机 29 个 API-key provider 当前都只有 `name`、`base_url`、`wire_api` 和
`requires_openai_auth` 四个字段，但 Codex schema 未来可增加 headers、重试、查询参数或
嵌套认证字段。把整个 provider 序列化到 argv 会扩大配置暴露面，也容易漏字段。

## 转换算法

`cc_switch_config.py` 继续先用 `tomllib` 验证：

1. `auth` 恰好只有字符串 `OPENAI_API_KEY`，值不含 NUL。
2. `model_provider` 是非空字符串。
3. `model_providers[model_provider]` 是对象。
4. 活动 provider 使用 `requires_openai_auth = true` 且没有 `env_key`。

然后在原始 TOML 文本中查找当前 CC Switch 格式的认证赋值行：

```python
_CODEX_OPENAI_AUTH_ASSIGNMENT = re.compile(
    r"(?m)^(?P<indent>[ \t]*)"
    r"requires_openai_auth[ \t]*=[ \t]*true"
    r"(?P<comment>[ \t]*(?:#.*)?)$"
)
```

对每个文本候选分别生成：

```toml
requires_openai_auth = false
env_key = "OPENAI_API_KEY"
```

保留原缩进及行尾注释，再使用 `tomllib` 解析候选。程序构造一份期望解析树：深拷贝原始树，
只把活动 provider 的 `requires_openai_auth` 改成 `False`，再加入
`env_key = "OPENAI_API_KEY"`。只有解析结果与期望树完全相等的候选才算匹配，并且必须恰好
有一个匹配。

这种“文本候选 + 解析树等价检查”不需要 TOML 写回库，同时保证注释、表顺序或配置中其他
同名字段不会导致误改。格式不受支持、没有匹配或不能唯一匹配时，抛出稳定的脱敏
`CcSwitchConfigError`。

本机全部 29 个单一 API-key 配置已用只读原型验证，每个配置都恰好得到一个语义等价候选。

## Runtime 与启动命令

`CodexProfileRuntime` 不再需要 `config_overrides`。`_prepare_codex_profile` 将转换后的 TOML
写入随机的 `cc-switch-*.config.toml`，权限仍为 `0600`；profile 中只出现环境变量名称
`OPENAI_API_KEY`，不出现其值。

`codex_tui.py` 恢复为：

```text
codex --profile <random-profile> <forwarded Codex arguments>
```

用户自己转发的 `-c/--config` 不受影响，仍位于原来的参数位置并拥有 Codex 定义的优先级。
所选 token 继续通过复制后的子进程环境覆盖 `OPENAI_API_KEY`，父环境不变。

## 错误与安全

- TOML 无效、认证结构不支持、活动 provider 缺失或不能唯一等价转换时，在 profile 写入和
  Codex 启动前返回退出码 `2`。
- 公共错误不包含配置正文、provider URL、token 或底层解析异常链。
- token 不进入转换后的 profile、argv、`CodexProfileRuntime` 或 `repr`。
- 原始 profile 中所有非认证语义必须保留；等价检查不通过时拒绝，而不是猜测性改写。
- 临时 profile 的正常、异常、Ctrl-C、SIGTERM 和清理失败行为保持不变。
- App 当前选择仅保留为共享基础配置层，不会被修改；转换后的完整临时 profile 可使用不同
  provider ID。

## 测试

使用 `unittest` 和 mock，不发送模型请求：

- 写出的 profile 重新解析后，只在活动 provider 上包含
  `requires_openai_auth = false` 和 `env_key = "OPENAI_API_KEY"`，其他解析树与原配置一致。
- profile 文本、runtime `repr` 和错误中不包含 token。
- 带注释和多个非活动 `requires_openai_auth = true` 候选时，只转换能产生期望解析树的活动
  provider 行。
- quoted provider ID、点号或空格 ID 不依赖 CLI path 编码，仍可通过语义转换。
- inline-table 等不符合当前文本格式但可解析的配置，在 `_write_private_file` 前被脱敏拒绝。
- TUI argv 不再包含自动生成的 provider `-c`；`resume SESSION_ID --model ...` 以及用户自带
  `-c` 的顺序保持不变。
- 子进程命令不含 token，环境包含所选 token。
- 完整测试、`py_compile` 和 `git diff --check` 必须通过。

额外执行无真实 token 的 Codex 0.145.0 兼容性检查：在私有临时 `CODEX_HOME` 中使用转换后
的 profile 运行 `codex --profile NAME mcp list`，预期配置成功加载且不发送模型请求。

## 文档一致性

旧 API-key 修复设计和实施计划会加入 superseded 提示，指向本设计，避免继续推荐 provider
CLI 子字段覆盖。README 的用户说明改为“认证字段写入临时 profile，token 仅注入子进程”，
不再声称通过 CLI 配置覆盖认证。

## 手动验收

CC Switch App 保持选择 `wuming`，依次运行：

```bash
python3 ~/to7for/nice/codex-candy-eval/codex_tui.py \
  --cc-switch-config muyuan

python3 ~/to7for/nice/codex-candy-eval/codex_tui.py \
  --cc-switch-config muyuan -- resume
```

预期不再出现 `provider name must not be empty` 或 `401 Invalid token`，请求使用 `muyuan`
URL 和 key；App 选择、已有窗口及共享 sessions 不变。
