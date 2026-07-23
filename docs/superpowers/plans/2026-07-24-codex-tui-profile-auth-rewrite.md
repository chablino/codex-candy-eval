# Codex TUI Temporary Profile Authentication Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken provider CLI authentication overrides with a semantically verified temporary-profile rewrite so any selected CC Switch API-key provider can authenticate independently of the App's current provider.

**Architecture:** Parse the selected TOML, validate the existing single-`OPENAI_API_KEY` provider shape, generate text-rewrite candidates for `requires_openai_auth = true`, and accept exactly one candidate whose reparsed tree differs only by `requires_openai_auth = false` and `env_key = "OPENAI_API_KEY"`. Write that transformed TOML to the existing owner-only temporary profile and launch Codex without generated provider `-c` arguments; the token remains only in the copied child environment.

**Tech Stack:** Python 3 standard library (`copy`, `re`, `tomllib`, `unittest`, `subprocess`), Codex CLI 0.145.0 profile loading.

---

### Task 1: Rewrite Profile Authentication and Remove Generated CLI Overrides

**Files:**
- Modify: `cc_switch_config.py`
- Modify: `codex_tui.py`
- Modify: `tests/test_cc_switch_config.py`
- Modify: `tests/test_codex_tui.py`

- [ ] **Step 1: Write failing tests for semantic profile transformation**

Add `tomllib` and `deepcopy` to `tests/test_cc_switch_config.py`. In
`test_profile_uses_shared_home_and_child_only_environment`, remove the old
`runtime.config_overrides` assertion and replace the exact-text assertion with a parsed semantic assertion:

```python
profile_text = profile_path.read_text(encoding="utf-8")
expected_profile = deepcopy(tomllib.loads(CODEX_SETTINGS["config"]))
expected_provider = expected_profile["model_providers"]["custom"]
expected_provider["requires_openai_auth"] = False
expected_provider["env_key"] = "OPENAI_API_KEY"

self.assertEqual(tomllib.loads(profile_text), expected_profile)
self.assertNotEqual(profile_text, CODEX_SETTINGS["config"])
self.assertNotIn("codex-test-secret", profile_text)
```

Replace `test_active_provider_id_is_toml_quoted_in_overrides` with a test that has two textual authentication candidates and a quoted active provider ID:

```python
def test_profile_rewrite_targets_only_the_active_provider(self):
    provider_id = "provider.with space"
    quoted_provider_id = json.dumps(provider_id, ensure_ascii=False)
    config = f'''\
model_provider = {quoted_provider_id}

[unrelated]
requires_openai_auth = true

[model_providers.{quoted_provider_id}]
name = "Selected"
base_url = "https://provider.example/v1"
wire_api = "responses"
requires_openai_auth = true # selected provider
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
            profile_path = self.codex_home / f"{runtime.profile_name}.config.toml"
            profile_text = profile_path.read_text(encoding="utf-8")
            parsed = tomllib.loads(profile_text)

            self.assertIs(parsed["unrelated"]["requires_openai_auth"], True)
            selected = parsed["model_providers"][provider_id]
            self.assertIs(selected["requires_openai_auth"], False)
            self.assertEqual(selected["env_key"], "OPENAI_API_KEY")
            self.assertIn("# selected provider", profile_text)
            self.assertNotIn("selected-secret", profile_text)
```

Append a parseable but non-line-rewritable provider to `unsupported_settings`:

```python
{
    "config": (
        'model_provider = "custom"\n'
        "model_providers.custom = { "
        'name = "custom", base_url = "https://provider.example/v1", '
        'wire_api = "responses", requires_openai_auth = true }\n'
    ),
    "auth": {"OPENAI_API_KEY": "secret"},
},
```

The existing loop must continue to assert `CcSwitchConfigError`, the substring
`single OPENAI_API_KEY`, no secret or exception chain, no `_write_private_file` call, and no profile file.

- [ ] **Step 2: Write failing launcher tests with no generated provider overrides**

In `tests/test_codex_tui.py`, remove `self.config_overrides` and construct the runtime without that field:

```python
self.runtime = CodexProfileRuntime(
    provider_id="provider-id",
    provider_name="jianzhile",
    profile_name="cc-switch-random",
    environment=self.environment,
)
```

Require the resume launch to preserve an explicit user override without adding authentication overrides:

```python
forwarded = [
    "resume",
    "session-id",
    "-c",
    "model_reasoning_effort=high",
    "--model",
    "gpt-5.6-sol",
]

run.assert_called_once_with(
    [
        "/usr/local/bin/codex",
        "--profile",
        "cc-switch-random",
        *forwarded,
    ],
    env=self.environment,
)
```

For the no-extra-arguments test, require exactly:

```python
run.assert_called_once_with(
    ["/bin/codex", "--profile", "cc-switch-random"],
    env=self.environment,
)
```

Retain the assertion that the command contains no `provider-secret` while the child environment does.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_cc_switch_config.CodexProfileRuntimeTests \
  tests.test_codex_tui -v
```

Expected: failures show that the profile still contains `requires_openai_auth = true`, the inline-table config is still accepted, and `codex_tui.py` still inserts the two generated `-c` pairs. The failures must be caused by the old behavior, not syntax or fixture errors.

- [ ] **Step 4: Implement parse helpers and semantically verified transformation**

In `cc_switch_config.py`, import `deepcopy`:

```python
from copy import deepcopy
```

Remove `config_overrides` from `CodexProfileRuntime`:

```python
@dataclass(frozen=True)
class CodexProfileRuntime:
    provider_id: str
    provider_name: str
    profile_name: str
    environment: Mapping[str, str] = field(repr=False)
```

Broaden the existing public error without exposing details, and define the current CC Switch assignment pattern:

```python
_CODEX_API_KEY_ERROR = (
    "Codex TUI launcher supports only adaptable CC Switch profiles with a "
    "single OPENAI_API_KEY"
)
_CODEX_OPENAI_AUTH_ASSIGNMENT = re.compile(
    r"(?m)^(?P<indent>[ \t]*)"
    r"requires_openai_auth[ \t]*=[ \t]*true"
    r"(?P<comment>[ \t]*(?:#.*)?)$"
)
```

Add a parser that consumes its own exceptions and returns no parser chain:

```python
def _parse_codex_toml(config: object) -> dict[str, Any] | None:
    if not isinstance(config, str):
        return None
    try:
        return tomllib.loads(config)
    except (tomllib.TOMLDecodeError, RecursionError):
        return None
```

Replace `_codex_api_key_runtime` with this complete helper:

```python
def _codex_api_key_profile(
    config: object, auth: object
) -> tuple[dict[str, str], str]:
    if (
        not isinstance(auth, dict)
        or set(auth) != {"OPENAI_API_KEY"}
        or not isinstance(auth.get("OPENAI_API_KEY"), str)
        or "\0" in auth["OPENAI_API_KEY"]
    ):
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    parsed = _parse_codex_toml(config)
    if parsed is None:
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

    expected = deepcopy(parsed)
    expected_provider = expected["model_providers"][provider_id]
    expected_provider["requires_openai_auth"] = False
    expected_provider["env_key"] = "OPENAI_API_KEY"

    transformed: list[str] = []
    assert isinstance(config, str)
    for match in _CODEX_OPENAI_AUTH_ASSIGNMENT.finditer(config):
        replacement = (
            f'{match.group("indent")}requires_openai_auth = false'
            f'{match.group("comment")}\n'
            f'{match.group("indent")}env_key = "OPENAI_API_KEY"'
        )
        candidate = config[: match.start()] + replacement + config[match.end() :]
        if _parse_codex_toml(candidate) == expected:
            transformed.append(candidate)

    if len(transformed) != 1:
        raise CcSwitchConfigError(_CODEX_API_KEY_ERROR)

    return {"OPENAI_API_KEY": auth["OPENAI_API_KEY"]}, transformed[0]
```

In `_prepare_codex_profile`, call it before resolving `CODEX_HOME`, write the returned `profile_config`, and construct the runtime without overrides:

```python
auth_environment, profile_config = _codex_api_key_profile(
    provider.settings.get("config"), provider.settings.get("auth")
)
environment = os.environ.copy()
home = _codex_home(environment)
profile_name = f"cc-switch-{uuid4().hex}"
profile_path = home / f"{profile_name}.config.toml"
_write_private_file(profile_path, profile_config)

environment.update(auth_environment)
environment.setdefault("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex-tui")
runtime = CodexProfileRuntime(
    provider_id=provider.provider_id,
    provider_name=provider.name,
    profile_name=profile_name,
    environment=environment,
)
```

- [ ] **Step 5: Remove generated authentication overrides from the launcher**

In `codex_tui.py`, delete `override_arguments` and restore the direct command:

```python
process = subprocess.run(
    [
        executable,
        "--profile",
        runtime.profile_name,
        *arguments.codex_args,
    ],
    env=runtime.environment,
)
```

- [ ] **Step 6: Run focused and full tests, then commit**

Run:

```bash
python3 -m unittest \
  tests.test_cc_switch_config.CodexProfileRuntimeTests \
  tests.test_codex_tui -v
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: all focused tests and the complete suite pass; no whitespace errors.

Commit:

```bash
git add cc_switch_config.py codex_tui.py \
  tests/test_cc_switch_config.py tests/test_codex_tui.py
git commit -m "fix: rewrite Codex TUI profile authentication"
```

### Task 2: Documentation, Codex Compatibility Check, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-24-cc-switch-codex-tui-api-key-auth-fix-design.md`
- Modify: `docs/superpowers/plans/2026-07-24-cc-switch-codex-tui-api-key-auth-fix.md`
- Modify: `docs/superpowers/specs/2026-07-24-codex-tui-profile-auth-rewrite-design.md`

- [ ] **Step 1: Mark the superseded CLI-override documents**

Immediately after the title in both old documents, add:

```markdown
> **Superseded:** The provider CLI override mechanism in this document is replaced by
> `docs/superpowers/specs/2026-07-24-codex-tui-profile-auth-rewrite-design.md` after
> Codex 0.145.0 compatibility testing exposed provider-map layering behavior.
```

Change the new design status to:

```text
状态：用户已复核，已实施
```

- [ ] **Step 2: Correct the README authentication explanation**

Replace the CLI-override paragraph with:

```markdown
交互启动器目前只支持认证对象中仅含一个 `OPENAI_API_KEY` 的 CC Switch Codex
中转站配置，不支持 `OpenAI Official` 等包含登录 tokens 的复杂认证配置；不支持的配置会在
启动 Codex 前安全退出。启动器会在权限为 `0600` 的临时 profile 中让所选 provider 从
该子进程的 `OPENAI_API_KEY` 读取认证；profile 只保存环境变量名称，不保存 token。因此
即使 App 当前选择其他 provider，也不会误用共享 `auth.json` 中的 token。
```

- [ ] **Step 3: Run a no-secret Codex 0.145.0 profile compatibility check**

Create an existing private temporary home:

```bash
mkdir -p /private/tmp/codex-candy-eval-profile-compat
chmod 700 /private/tmp/codex-candy-eval-profile-compat
```

From the repository root, run the production context manager with a dummy key and a provider that is absent from the empty base config:

```bash
CODEX_HOME=/private/tmp/codex-candy-eval-profile-compat python3 -c $'import subprocess\nfrom cc_switch_config import SelectedProvider, materialize_codex_profile\nconfig = "model_provider = \\\"isolated\\\"\\nmodel = \\\"gpt-test\\\"\\n\\n[model_providers.isolated]\\nname = \\\"Isolated\\\"\\nbase_url = \\\"https://example.invalid/v1\\\"\\nwire_api = \\\"responses\\\"\\nrequires_openai_auth = true\\n"\nprovider = SelectedProvider("db-id", "codex", "isolated", {"config": config, "auth": {"OPENAI_API_KEY": "dummy-test-key"}})\nwith materialize_codex_profile(provider) as runtime:\n    result = subprocess.run(["codex", "--profile", runtime.profile_name, "mcp", "list"], env=runtime.environment)\nraise SystemExit(result.returncode)'
```

Expected: exit `0` with `No MCP servers configured yet`; no `provider name must not be empty`, no API request, and the temporary `cc-switch-*.config.toml` is removed when the context exits.

- [ ] **Step 4: Run final verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_tui.py codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
git status --short
```

Expected: all tests pass, compilation and whitespace checks exit `0`, and status contains only the four planned documentation files.

- [ ] **Step 5: Commit documentation and inspect the branch**

```bash
git add README.md \
  docs/superpowers/specs/2026-07-24-cc-switch-codex-tui-api-key-auth-fix-design.md \
  docs/superpowers/plans/2026-07-24-cc-switch-codex-tui-api-key-auth-fix.md \
  docs/superpowers/specs/2026-07-24-codex-tui-profile-auth-rewrite-design.md
git commit -m "docs: correct Codex TUI authentication design"
git log --oneline main..HEAD
git status --short --branch
```

Expected: the design, implementation plan, code fix, and corrected documentation commits are present; the worktree is clean and ready for local fast-forward integration.

