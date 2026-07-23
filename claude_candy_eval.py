#!/usr/bin/env python3
"""用本地 claude CLI（Claude Code）测试糖果问题并判分。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""

# 正确答案为 21：只要回答中出现独立的 "21"（前后非数字）即判为正确。
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")


def resolve_claude_executable() -> str:
    """找到可被 subprocess 直接启动的 claude 命令。

    Windows/npm 安装通常会同时生成 `claude`、`claude.cmd` 等入口。
    `shutil.which("claude")` 可能先命中无扩展名 shim，CreateProcess 无法直接运行，
    会报 WinError 193；因此 Windows 上优先选择 `.cmd`/`.exe`。
    """
    candidates = (
        ("claude.cmd", "claude.exe", "claude")
        if os.name == "nt"
        else ("claude",)
    )
    for name in candidates:
        exe = shutil.which(name)
        if exe:
            return exe
    raise RuntimeError("找不到 claude 可执行文件，请确认已安装并加入 PATH。")


def run_claude(
    model: str | None,
    effort: str | None,
    *,
    environment: dict[str, str] | None = None,
    redact=None,
):
    exe = resolve_claude_executable()

    cmd = [
        exe, "-p", "--output-format", "json",
        # 不把会话落盘，评测运行不进入 /resume 历史；不影响 system prompt。
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    # 多行题目通过 stdin 传入：作为命令行参数时，经 cmd.exe/claude.cmd 包装后换行
    # 可能被吞掉，而管道里的内容能完整保留。claude -p 在无位置参数时读 stdin。
    proc = subprocess.run(
        cmd,
        input=PROMPT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        stdout = proc.stdout or ""
        if redact is not None:
            stderr = redact(stderr)
            stdout = redact(stdout)
        raise RuntimeError(stderr.strip() or stdout.strip() or "claude failed")

    data = json.loads(proc.stdout)
    if data.get("is_error"):
        result = str(data.get("result") or "")
        subtype = str(data.get("subtype") or "")
        if redact is not None:
            result = redact(result)
            subtype = redact(subtype)
        raise RuntimeError(result or subtype or "claude error")

    usage = data.get("usage") or {}
    # 实际发送的完整提示 = 未命中缓存 + 写缓存 + 读缓存三部分之和。
    in_tok = sum(
        usage.get(key) or 0
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    out_tok = usage.get("output_tokens") or 0
    if not in_tok and not out_tok:
        # 偶发情况下 usage 会是全 0；退回 modelUsage，取主模型（输入 tokens 最多的
        # 一项，其余是 Claude Code 内部辅助调用，如 haiku 分类器）的统计。
        main = max(
            (data.get("modelUsage") or {}).values(),
            default=None,
            key=lambda u: (u.get("inputTokens") or 0) + (u.get("cacheReadInputTokens") or 0),
        )
        if main:
            in_tok = sum(
                main.get(key) or 0
                for key in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
            )
            out_tok = main.get("outputTokens") or 0
    return data.get("result", ""), in_tok or None, out_tok or None


def char_width(char: str) -> int:
    """终端显示宽度：组合字符 0，东亚全角/宽字符 2，其余 1。"""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def pad(text: str, width: int, align: str) -> str:
    """按显示宽度补空格对齐（中文宽字符按 2 计）。"""
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def render_table(headers: list[str], rows: list[list], aligns: list[str]) -> str:
    """原生渲染对齐表格（tabulate "simple" 风格），列宽按显示宽度计算。"""
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [
        max(display_width(headers[i]), *(display_width(r[i]) for r in str_rows)) if str_rows
        else display_width(headers[i])
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(pad(cells[i], widths[i], aligns[i]) for i in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in str_rows]
    return "\n".join(lines)


def preview(text: str, limit: int = 40) -> str:
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    if display_width(flat) <= limit:
        return flat

    result = []
    width = 0
    for char in flat:
        next_width = char_width(char)
        if width + next_width > limit - 3:
            break
        result.append(char)
        width += next_width
    return "".join(result) + "..."


def _enable_windows_ansi() -> bool:
    """开启 Windows 控制台的 VT 处理，让 ANSI 转义序列（含光标定位）生效。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:
        return False


def setup_console() -> bool:
    """统一输出为 UTF-8（避免 ✓/✗、中文在 GBK 等控制台编码下抛 UnicodeEncodeError），
    并探测是否可用 ANSI 光标控制做表格原地刷新。

    返回 True 表示可原地重绘；否则（如输出被重定向、或旧版 Windows 无法开 VT）退化为
    结束后一次性打印整张表，避免屏幕上出现 `←[s` 之类的转义乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return _enable_windows_ansi()
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--model", help="模型名或别名（如 fable/opus/sonnet）；缺省用 Claude Code 默认模型。")
    parser.add_argument(
        "-r", "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
        help="传给 claude --effort；缺省用 Claude Code 默认档位。",
    )
    parser.add_argument("-n", "--tests", type=int, default=1)
    parser.add_argument(
        "--cc-switch-config",
        metavar="NAME_OR_ID",
        help="CC Switch Claude config name or ID; does not change the App selection.",
    )
    return parser.parse_args(argv)


def _evaluate(args, use_ansi: bool, runtime) -> None:
    environment = runtime.environment if runtime is not None else None
    redactor = runtime.redact if runtime is not None else None

    headers = ["Run", "Claude", "In Tok", "Out Tok", "Time(s)", "TPS", "OK"]
    aligns = ["right", "left", "right", "right", "right", "right", "center"]

    def run_one(index: int) -> tuple[list, bool | None]:
        try:
            start = time.perf_counter()
            text, in_tok, out_tok = run_claude(
                args.model,
                args.reasoning_effort,
                environment=environment,
                redact=redactor,
            )
            elapsed = time.perf_counter() - start
            tps = out_tok / elapsed if out_tok and elapsed > 0 else None
            ok = bool(ANSWER_PATTERN.search(text))
            return [index, preview(text), in_tok, out_tok, f"{elapsed:.1f}",
                    f"{tps:.1f}" if tps else "-", "✓" if ok else "✗"], ok
        except Exception as exc:
            return [index, f"ERROR: {preview(str(exc))}", *["-"] * 5], None

    # 串行执行：逐个请求，完成一个立即打印该行结果。
    rows = []
    graded = []
    prev_lines = 0  # 上一次绘制的表格占据的屏幕行数，用于原地重绘时上移光标
    for index in range(1, args.tests + 1):
        row, ok = run_one(index)
        rows.append(row)
        if ok is not None:
            graded.append(ok)
        if use_ansi:
            # 用“行数计数 + 光标上移（CSI A）”替代 save/restore（CSI s/u）。
            # macOS Terminal.app 不支持 CSI s/u，会导致表格每轮向下堆叠、表头重复；
            # 光标上移序列所有常见终端都支持，最稳妥。
            if prev_lines > 0:
                sys.stdout.write(f"\033[{prev_lines}A\033[J")
            table = render_table(headers, rows, aligns)
            sys.stdout.write(table + "\n")
            sys.stdout.flush()
            prev_lines = table.count("\n") + 1
    if not use_ansi:
        print(render_table(headers, rows, aligns), flush=True)

    correct = sum(graded)
    print(f"\nGraded {len(graded)}/{args.tests}  correct={correct}  "
          f"accuracy={correct / len(graded) * 100:.1f}%"
          if graded else f"\nGraded 0/{args.tests}")


def main(argv=None) -> int:
    use_ansi = setup_console()
    args = parse_args(argv)
    if args.cc_switch_config is None:
        _evaluate(args, use_ansi, None)
        return 0

    try:
        from cc_switch_config import CcSwitchConfigError, use_provider
    except ModuleNotFoundError as exc:
        if exc.name != "cc_switch_config":
            raise
        print(
            "ERROR: --cc-switch-config requires the complete repository",
            file=sys.stderr,
        )
        return 2

    try:
        with use_provider("claude", args.cc_switch_config) as runtime:
            _evaluate(args, use_ansi, runtime)
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
