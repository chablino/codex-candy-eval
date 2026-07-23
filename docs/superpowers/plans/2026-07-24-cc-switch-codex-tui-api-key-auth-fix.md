# CC Switch Codex TUI API-key Authentication Fix Implementation Plan

> **Superseded:** The provider CLI override mechanism in this document is replaced by
> `docs/superpowers/specs/2026-07-24-codex-tui-profile-auth-rewrite-design.md` after
> Codex 0.145.0 compatibility testing exposed provider-map layering behavior.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `codex_tui.py --cc-switch-config PROVIDER` authenticate with the selected CC Switch API key even when the App currently selects a different provider.

**Architecture:** Parse the selected provider's TOML before creating its temporary profile, accept only the current CC Switch single-`OPENAI_API_KEY` provider shape, and add two token-free Codex CLI configuration overrides to the runtime contract. The launcher inserts those overrides after its temporary `--profile` and before user-forwarded arguments, so Codex reads the selected key from the child-only environment while continuing to share the existing `CODEX_HOME` and sessions.

**Tech Stack:** Python 3 standard library (`tomllib`, `json`, `dataclasses`, `subprocess`), `unittest`, Codex CLI configuration overrides.

---

### Task 1: Validate API-key Providers and Build Token-free Overrides

**Files:**
- Modify: `cc_switch_config.py`
- Modify: `tests/test_cc_switch_config.py`

- [ ] **Step 1: Give the Codex test fixture a real active provider table**

Replace the minimal `CODEX_SETTINGS["config"]` fixture with valid TOML that models CC Switch's current API-key shape:

```python
CODEX_SETTINGS = {
    "config": """\
model = "gpt-test"
model_provider = "custom"

[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
requires_openai_auth = true
""",
    "auth": {"OPENAI_API_KEY": "codex-test-secret"},
}
```

- [ ] **Step 2: Write failing runtime tests for the selected key and CLI overrides**

Extend `CodexProfileRuntimeTests.test_profile_uses_shared_home_and_child_only_environment` so the existing shared `auth.json` clearly belongs to provider A, then assert the runtime uses provider B without changing shared state:

```python
default_auth.write_text(
    '{"OPENAI_API_KEY":"app-selected-secret"}\n', encoding="utf-8"
)

self.assertEqual(
    runtime.config_overrides,
    (
        'model_providers."custom".requires_openai_auth=false',
        'model_providers."custom".env_key="OPENAI_API_KEY"',
    ),
)
self.assertEqual(
    runtime.environment["OPENAI_API_KEY"], "codex-test-secret"
)
self.assertEqual(
    default_auth.read_text(encoding="utf-8"),
    '{"OPENAI_API_KEY":"app-selected-secret"}\n',
)
self.assertNotIn("codex-test-secret", repr(runtime))
```

Add a table-driven test proving active provider IDs are quoted as one TOML dotted-key segment for both observed IDs and a punctuation case:

```python
def test_active_provider_id_is_toml_quoted_in_overrides(self):
    for provider_id in ("custom", "hlool", "provider.with space"):
        with self.subTest(provider_id=provider_id):
            config = f'''\
model_provider = {json.dumps(provider_id)}

[model_providers.{json.dumps(provider_id)}]
requires_openai_auth = true
'''
            provider = SelectedProvider(
                "db-id",
                "codex",
                "provider",
                {
                    "config": config,
                    "auth": {"OPENAI_API_KEY": "selected-secret"},
                },
            )
            with mock.patch.dict(
                os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=True
            ):
                with materialize_codex_profile(provider) as runtime:
                    quoted = json.dumps(provider_id, ensure_ascii=False)
                    self.assertEqual(
                        runtime.config_overrides,
                        (
                            f"model_providers.{quoted}.requires_openai_auth=false",
                            f'model_providers.{quoted}.env_key="OPENAI_API_KEY"',
                        ),
                    )
                    self.assertNotIn("selected-secret", runtime.config_overrides)
```

- [ ] **Step 3: Write failing rejection tests for unsupported authentication/config shapes**

Replace the old generic-auth acceptance assumptions with a test table containing:

```python
unsupported_settings = (
    {"config": CODEX_SETTINGS["config"], "auth": {}},
    {"config": CODEX_SETTINGS["config"], "auth": {"OTHER_KEY": "secret"}},
    {
        "config": CODEX_SETTINGS["config"],
        "auth": {"tokens": {"access_token": "secret"}},
    },
    {
        "config": CODEX_SETTINGS["config"],
        "auth": {"OPENAI_API_KEY": "secret", "EXTRA": "secret"},
    },
    {"config": CODEX_SETTINGS["config"], "auth": {"OPENAI_API_KEY": 1}},
    {"config": "not valid = [", "auth": {"OPENAI_API_KEY": "secret"}},
    {"config": 'model = "gpt-test"\n', "auth": {"OPENAI_API_KEY": "secret"}},
    {"config": 'model_provider = ""\n', "auth": {"OPENAI_API_KEY": "secret"}},
    {"config": 'model_provider = 1\n', "auth": {"OPENAI_API_KEY": "secret"}},
    {
        "config": 'model_provider = "custom"\n',
        "auth": {"OPENAI_API_KEY": "secret"},
    },
    {
        "config": 'model_provider = "custom"\nmodel_providers = "bad"\n',
        "auth": {"OPENAI_API_KEY": "secret"},
    },
    {
        "config": 'model_provider = "custom"\nmodel_providers.custom = "bad"\n',
        "auth": {"OPENAI_API_KEY": "secret"},
    },
    {
        "config": 'model_provider = "custom"\n[model_providers.custom]\nrequires_openai_auth = false\n',
        "auth": {"OPENAI_API_KEY": "secret"},
    },
    {
        "config": 'model_provider = "custom"\n[model_providers.custom]\nrequires_openai_auth = true\nenv_key = "OPENAI_API_KEY"\n',
        "auth": {"OPENAI_API_KEY": "secret"},
    },
)
```

For every case, patch `_write_private_file`, call `materialize_codex_profile`, and assert:

```python
self.assertIn("single OPENAI_API_KEY", str(raised.exception))
self.assertNotIn("secret", str(raised.exception))
self.assertIsNone(raised.exception.__cause__)
self.assertIsNone(raised.exception.__context__)
write_private_file.assert_not_called()
self.assertEqual(self.profile_paths(), [])
```

This includes the complex-auth shape used by OpenAI Official and verifies rejection happens before a profile can be written.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_cc_switch_config.CodexProfileRuntimeTests -v
```

Expected: failures because `CodexProfileRuntime` has no `config_overrides`, unsupported configurations are still accepted, and the old fixture no longer satisfies the intended authentication behavior.

- [ ] **Step 5: Add TOML parsing and the runtime contract**

Import `tomllib` and add the field:

```python
@dataclass(frozen=True)
class CodexProfileRuntime:
    provider_id: str
    provider_name: str
    profile_name: str
    config_overrides: tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False)
```

Add a private helper whose public failures contain no parser details or secret values:

```python
_CODEX_API_KEY_ERROR = (
    "Codex TUI launcher supports only CC Switch providers with a single "
    "OPENAI_API_KEY"
)


def _codex_api_key_runtime(
    config: object, auth: object
) -> tuple[dict[str, str], tuple[str, ...]]:
    if (
        not isinstance(auth, dict)
        or set(auth) != {"OPENAI_API_KEY"}
        or not isinstance(auth.get("OPENAI_API_KEY"), str)
        or "\0" in auth["OPENAI_API_KEY"]
    ):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    parsed: object = None
    if isinstance(config, str):
        try:
            parsed = tomllib.loads(config)
        except (tomllib.TOMLDecodeError, RecursionError):
            parsed = None

    if not isinstance(parsed, dict):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    provider_id = parsed.get("model_provider")
    providers = parsed.get("model_providers")
    provider_config = (
        providers.get(provider_id)
        if isinstance(provider_id, str) and isinstance(providers, dict)
        else None
    )
    if (
        not isinstance(provider_id, str)
        or not provider_id.strip()
        or not isinstance(provider_config, dict)
        or provider_config.get("requires_openai_auth") is not True
        or "env_key" in provider_config
    ):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    quoted_provider_id = json.dumps(provider_id, ensure_ascii=False)
    prefix = f"model_providers.{quoted_provider_id}"
    return (
        {"OPENAI_API_KEY": auth["OPENAI_API_KEY"]},
        (
            f"{prefix}.requires_openai_auth=false",
            f'{prefix}.env_key="OPENAI_API_KEY"',
        ),
    )
```

Call this helper at the start of `_prepare_codex_profile`, before `_codex_home` and `_write_private_file`, and pass its second result into `CodexProfileRuntime(config_overrides=...)`. Keep the selected token only in the copied `environment` and preserve the existing originator behavior.

- [ ] **Step 6: Run focused and full tests, then commit**

Run:

```bash
python3 -m unittest tests.test_cc_switch_config.CodexProfileRuntimeTests -v
python3 -m unittest discover -s tests -v
```

Expected: all profile-runtime tests and the full suite pass.

Commit:

```bash
git add cc_switch_config.py tests/test_cc_switch_config.py
git commit -m "fix: select Codex TUI API-key authentication"
```

### Task 2: Pass Authentication Overrides to Codex TUI

**Files:**
- Modify: `codex_tui.py`
- Modify: `tests/test_codex_tui.py`

- [ ] **Step 1: Update the mocked runtime and write failing command-order tests**

Construct the fixture with token-free overrides:

```python
self.runtime = CodexProfileRuntime(
    provider_id="provider-id",
    provider_name="jianzhile",
    profile_name="cc-switch-random",
    config_overrides=(
        'model_providers."custom".requires_openai_auth=false',
        'model_providers."custom".env_key="OPENAI_API_KEY"',
    ),
    environment=self.environment,
)
```

Change the resume assertion to require this exact order:

```python
run.assert_called_once_with(
    [
        "/usr/local/bin/codex",
        "--profile",
        "cc-switch-random",
        "-c",
        'model_providers."custom".requires_openai_auth=false',
        "-c",
        'model_providers."custom".env_key="OPENAI_API_KEY"',
        *forwarded,
    ],
    env=self.environment,
)
```

Retain `forwarded = ["resume", "session-id", "--model", "gpt-5.6-sol"]`, and update the no-extra-arguments assertion similarly. Inspect `run.call_args.args[0]` and assert `"provider-secret"` is absent while `run.call_args.kwargs["env"]` contains it. Existing error, child return-code, cleanup, and profile-conflict tests stay unchanged.

- [ ] **Step 2: Run launcher tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_codex_tui -v
```

Expected: launch assertions fail because `codex_tui.py` does not yet add `runtime.config_overrides` to the command.

- [ ] **Step 3: Insert generated overrides before all forwarded arguments**

Inside the runtime context, flatten each override to one `-c VALUE` pair and build the process command:

```python
override_arguments = [
    argument
    for override in runtime.config_overrides
    for argument in ("-c", override)
]
process = subprocess.run(
    [
        executable,
        "--profile",
        runtime.profile_name,
        *override_arguments,
        *arguments.codex_args,
    ],
    env=runtime.environment,
)
```

Do not add token values to the command, capture terminal streams, or reorder the user's `resume`, session ID, `--model`, or `-c/--config` arguments.

- [ ] **Step 4: Run focused and full tests, then commit**

Run:

```bash
python3 -m unittest tests.test_codex_tui -v
python3 -m unittest discover -s tests -v
```

Expected: all launcher tests and the full suite pass.

Commit:

```bash
git add codex_tui.py tests/test_codex_tui.py
git commit -m "fix: pass selected API key to Codex TUI"
```

### Task 3: Document the Supported Authentication Scope and Verify

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document API-key-only TUI selection**

After the shared-session explanation, add this scope note:

```markdown
交互启动器目前只支持认证对象中仅含一个 `OPENAI_API_KEY` 的 CC Switch Codex
中转站配置，不支持 `OpenAI Official` 等包含登录 tokens 的复杂认证配置；不支持的配置会在
启动 Codex 前安全退出。启动器会通过不含 token 的 Codex 配置覆盖，让所选 provider 从
该子进程的 `OPENAI_API_KEY` 读取认证，因此即使 App 当前选择其他 provider，也不会误用
共享 `auth.json` 中的 token。
```

- [ ] **Step 2: Run final verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_tui.py codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
git status --short
```

Expected: all tests pass, compilation and whitespace checks produce no errors, and status contains only the planned README change before its commit.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md
git commit -m "docs: clarify Codex TUI authentication support"
```

- [ ] **Step 4: Inspect the final branch before integration**

Run:

```bash
git log --oneline main..HEAD
git diff --stat main...HEAD
git status --short --branch
```

Expected: the design, plan, runtime fix, launcher fix, and documentation commits are present; the worktree is clean.
