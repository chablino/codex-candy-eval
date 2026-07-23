# Codex Originator Default 设计

日期：2026-07-23

状态：用户已确认

## 背景

部分中转站会校验 Codex 客户端来源。直接运行三个 Codex 评测脚本时，子进程可能因为缺少 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` 而失败；在命令前手动设置 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=codex-tui` 后可以正常请求。用户希望评测脚本默认补齐该变量，同时保留手动指定其他来源值的能力。

## 行为

- `codex_candy_eval.py`、`codex_tps_eval.py` 和 `codex_juice_eval.py` 启动 Codex 子进程时，若环境中不存在 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`，则设置为 `codex-tui`。
- 若调用者已显式设置该变量，无论值为何，都原样保留。
- 普通运行从当前进程环境复制子进程环境；使用 `--cc-switch-config` 时从所选 runtime 环境复制。
- 保留 `PATH`、`CODEX_HOME` 及其他已有环境变量。
- 不修改 `os.environ`，也不修改传入的 CC Switch runtime 环境映射。
- `claude_candy_eval.py` 和 `opencode_candy_eval.py` 不受影响。

优先级如下：

```text
显式 CODEX_INTERNAL_ORIGINATOR_OVERRIDE
  > 脚本默认值 codex-tui
```

## 实现

三个 Codex 子进程函数分别在调用 `subprocess.run` 前构造环境副本：未提供 `environment` 时复制 `os.environ`，已提供时复制该映射，然后用 `setdefault` 补入默认值。小段逻辑保留在各独立脚本中，避免引入共享模块后破坏单文件运行方式。

## 测试

标准库 `unittest` 使用 mock 检查，不发送真实模型请求：

- 三个 Codex 入口均在缺失变量时传入 `codex-tui`。
- 当前环境已有自定义值时予以保留。
- CC Switch 环境中的 `CODEX_HOME` 等键保持不变。
- 传入的环境映射没有被修改。
- Claude 子进程环境行为保持原样。

## 验收标准

1. 用户可以直接运行三个 Codex 评测脚本，无需每次输入环境变量前缀。
2. `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=custom python ...` 仍让 Codex 子进程收到 `custom`。
3. 普通模式和 `--cc-switch-config` 模式行为一致。
4. 完整单元测试、Python 编译检查和 `git diff --check` 通过。
