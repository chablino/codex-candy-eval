# Codex Default Plugin Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Codex provider a stable, project-owned snapshot of the eight plugins enabled by the current anyrouter configuration, while preserving provider, Common Config, and provider-sidecar override semantics.

**Architecture:** Add a strict `codex_plugin_defaults.toml` beside `codex_runtime.py`. The runtime loader validates this file and passes a boolean mapping into `compose_effective_config`; composition treats installed defaults as the lowest plugin layer, then applies provider/Common Config and finally the provider sidecar. Inventory remains the source of installation truth, so missing default caches are silently omitted and no database is written.

**Tech Stack:** Python 3.11+, `tomllib`, `tomli_w`, `unittest`, existing Codex runtime and plugin-state helpers.

---

### Task 1: Add the fixed default snapshot

**Files:**
- Create: `codex_plugin_defaults.toml`
- Test: `tests/test_codex_runtime.py`

- [ ] **Step 1: Write the failing snapshot test**

Add a `DefaultPluginConfigTests` class to `tests/test_codex_runtime.py` that imports `_load_default_plugin_config` and asserts the project file yields exactly these eight canonical IDs, all `True`:

```python
EXPECTED_DEFAULT_PLUGINS = {
    "superpowers@openai-api-curated",
    "documents@openai-primary-runtime",
    "pdf@openai-primary-runtime",
    "presentations@openai-primary-runtime",
    "template-creator@openai-primary-runtime",
    "spreadsheets@openai-primary-runtime",
    "visualize@openai-bundled",
    "browser@openai-bundled",
}

class DefaultPluginConfigTests(unittest.TestCase):
    def test_project_defaults_are_the_eight_anyrouter_plugins(self):
        defaults = codex_runtime._load_default_plugin_config()
        self.assertEqual(set(defaults), EXPECTED_DEFAULT_PLUGINS)
        self.assertTrue(all(defaults.values()))
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python3 -m pytest tests/test_codex_runtime.py::DefaultPluginConfigTests::test_project_defaults_are_the_eight_anyrouter_plugins -q`

Expected: FAIL because `_load_default_plugin_config` and `codex_plugin_defaults.toml` do not exist yet.

- [ ] **Step 3: Create the snapshot file**

Create `codex_plugin_defaults.toml` with only the following tables:

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

- [ ] **Step 4: Run the focused test again**

Run the command from Step 2. Expected: still FAIL until the loader is implemented; keep this as the red test for Task 2.

- [ ] **Step 5: Commit the fixture and test**

Run: `git add tests/test_codex_runtime.py codex_plugin_defaults.toml && git commit -m "test: define Codex default plugin snapshot"`

### Task 2: Implement strict default-file loading

**Files:**
- Modify: `codex_runtime.py:1-150`
- Test: `tests/test_codex_runtime.py`

- [ ] **Step 1: Add failure-mode tests**

Extend `DefaultPluginConfigTests` with a temporary-file helper that patches `codex_runtime._DEFAULT_PLUGIN_CONFIG_PATH` and tests missing file, symlink, directory, malformed TOML, extra top-level key, extra plugin field, and non-boolean `enabled`. Each case must raise `CcSwitchConfigError`, and each message must not contain the secret marker used in the invalid payload.

Use concrete cases such as:

```python
def test_invalid_default_files_are_sanitized(self):
    cases = {
        "bad = [": "bad",
        "[plugins]\n\"demo@test\" = {enabled = true, extra = \"secret\"}\n": "secret",
        "[plugins.\"demo@test\"]\nenabled = \"secret\"\n": "secret",
        "[other]\nvalue = \"secret\"\n": "secret",
    }
    for payload, marker in cases.items():
        with self.subTest(payload=payload):
            path = self.root / "defaults.toml"
            path.write_text(payload, encoding="utf-8")
            with mock.patch.object(codex_runtime, "_DEFAULT_PLUGIN_CONFIG_PATH", path):
                with self.assertRaises(CcSwitchConfigError) as raised:
                    codex_runtime._load_default_plugin_config()
            self.assertNotIn(marker, str(raised.exception))
```

Also assert a symlink and directory are rejected, and a missing file raises the same sanitized configuration error.

- [ ] **Step 2: Run the failure-mode tests**

Run: `python3 -m pytest tests/test_codex_runtime.py::DefaultPluginConfigTests -q`

Expected: FAIL because no loader or validation exists.

- [ ] **Step 3: Implement `_load_default_plugin_config`**

Near the existing TOML helpers in `codex_runtime.py`, define:

```python
_DEFAULT_PLUGIN_CONFIG_PATH = Path(__file__).with_name("codex_plugin_defaults.toml")

def _load_default_plugin_config() -> dict[str, bool]:
    path = _DEFAULT_PLUGIN_CONFIG_PATH
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("default plugin config is not a regular file")
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, RecursionError):
        raise CcSwitchConfigError("Codex default plugin configuration is invalid") from None

    if set(document) != {"plugins"} or not isinstance(document["plugins"], Mapping):
        raise CcSwitchConfigError("Codex default plugin configuration is invalid")
    defaults: dict[str, bool] = {}
    for plugin_id, entry in document["plugins"].items():
        if not isinstance(plugin_id, str) or not isinstance(entry, Mapping):
            raise CcSwitchConfigError("Codex default plugin configuration is invalid")
        if set(entry) != {"enabled"} or not isinstance(entry["enabled"], bool):
            raise CcSwitchConfigError("Codex default plugin configuration is invalid")
        canonical_id = normalize_plugin_id(plugin_id)
        if not plugin_id or "\0" in plugin_id:
            raise CcSwitchConfigError("Codex default plugin configuration is invalid")
        if plugin_id == LEGACY_SUPERPOWERS_ID and CANONICAL_SUPERPOWERS_ID in defaults:
            continue
        defaults[canonical_id] = entry["enabled"]
    return defaults
```

Import `CANONICAL_SUPERPOWERS_ID`, `LEGACY_SUPERPOWERS_ID`, and `normalize_plugin_id`. Reject empty IDs and IDs containing NUL. When both Superpowers spellings occur, the canonical spelling wins regardless of TOML order.

- [ ] **Step 4: Run the focused tests**

Run: `python3 -m pytest tests/test_codex_runtime.py::DefaultPluginConfigTests -q`

Expected: PASS.

- [ ] **Step 5: Commit the loader**

Run: `git add codex_runtime.py tests/test_codex_runtime.py && git commit -m "feat: validate Codex default plugin settings"`

### Task 3: Compose defaults at the lowest plugin layer

**Files:**
- Modify: `codex_plugin_state.py:116-170`
- Test: `tests/test_codex_plugin_state.py`

- [ ] **Step 1: Add red composition tests**

Extend `EffectiveConfigTests` with tests that pass `default_plugins=`:

```python
def test_installed_defaults_are_enabled_and_enter_baseline(self):
    result = compose_effective_config(
        {}, None, False, {}, {"demo@test"}, {}, {"demo@test": True}
    )
    self.assertTrue(plugin_flags(result.document)["demo@test"])
    self.assertEqual(result.baseline_plugins, {"demo@test": True})

def test_default_missing_from_inventory_is_silent(self):
    result = compose_effective_config(
        {}, None, False, {}, set(), {}, {"missing@test": True}
    )
    self.assertNotIn("missing@test", result.document.get("plugins", {}))
    self.assertEqual(result.warnings, ())

def test_plugin_precedence_is_default_provider_common_then_sidecar(self):
    result = compose_effective_config(
        {"plugins": {"demo@test": {"enabled": False, "provider": True}}},
        {"plugins": {"demo@test": {"enabled": True, "common": True}}},
        True, {}, {"demo@test"}, {"demo@test": False}, {"demo@test": True},
    )
    self.assertFalse(plugin_flags(result.document)["demo@test"])
    self.assertTrue(result.document["plugins"]["demo@test"]["provider"])
    self.assertTrue(result.document["plugins"]["demo@test"]["common"])
    self.assertTrue(result.baseline_plugins["demo@test"])
```

Add a canonical/legacy default-ID test if it is not covered by the loader tests.

- [ ] **Step 2: Run the new state tests and confirm failure**

Run: `python3 -m pytest tests/test_codex_plugin_state.py::EffectiveConfigTests -q`

Expected: FAIL because the function does not accept or apply `default_plugins`.

- [ ] **Step 3: Implement the new optional layer**

Change the signature to append `default_plugins: Mapping[str, bool] | None = None`. Normalize and validate it with `_normalized_plugin_overrides`. Build `default_entries` only for normalized IDs in inventory, each as `{"enabled": enabled}`. Start `document` from `deep_merge({"plugins": default_entries}, provider)` before the existing Common Config merge, preserving non-plugin provider fields. Then normalize the merged document, compute `baseline_plugins` after the Common Config merge and before sidecar overrides, inject remaining inventory IDs as disabled, and apply the existing `provider_plugins` last. Ensure the returned baseline includes installed defaults plus provider/Common declarations, but never sidecar overrides.

The core ordering must be equivalent to:

```python
normalized_inventory = _normalized_inventory(inventory)
defaults = {
    plugin_id: {"enabled": enabled}
    for plugin_id, enabled in _normalized_plugin_overrides(default_plugins or {}).items()
    if plugin_id in normalized_inventory
}
document = deep_merge({"plugins": defaults}, provider)
if common_enabled and common is not None:
    document = deep_merge(document, common)
plugins = _normalized_plugin_entries(document)
baseline_plugins = {
    plugin_id: entry.get("enabled", True)
    for plugin_id, entry in plugins.items()
}
```

Keep existing warning behavior for provider/Common explicit enabled plugins missing from inventory. Defaults filtered before document construction must not warn.

- [ ] **Step 4: Run all plugin-state tests**

Run: `python3 -m pytest tests/test_codex_plugin_state.py -q`

Expected: PASS, including existing sidecar, reset, inventory removal, and concurrency coverage.

- [ ] **Step 5: Commit composition changes**

Run: `git add codex_plugin_state.py tests/test_codex_plugin_state.py && git commit -m "feat: layer installed Codex plugin defaults"`

### Task 4: Wire defaults into runtime materialization

**Files:**
- Modify: `codex_runtime.py:252-320`
- Test: `tests/test_codex_runtime.py`

- [ ] **Step 1: Add runtime red tests**

Add a test that installs all eight fixture manifests into the temporary shared home, starts a provider with no plugin declarations and no sidecar, and asserts the generated `config.toml` enables all eight IDs. Add tests that a default plugin absent from inventory is omitted without warnings, and that a provider/Common Config `enabled = false` plus a sidecar `true` follows the documented precedence.

- [ ] **Step 2: Run the runtime tests and confirm failure**

Run: `python3 -m pytest tests/test_codex_runtime.py::CodexRuntimeMaterializationTests -q`

Expected: the new default-enabled assertions fail because runtime does not load or pass defaults.

- [ ] **Step 3: Load and pass the defaults**

In `_prepare_codex_runtime`, call `_load_default_plugin_config()` after shared/provider/common TOML parsing and before `compose_effective_config`. Pass `default_plugins` as the final keyword argument. Do not read the anyrouter provider or current CC Switch selection.

```python
default_plugins = _load_default_plugin_config()
...
composed = compose_effective_config(
    provider_document,
    common_document,
    common_document is not None,
    launcher_marketplaces,
    inventory,
    provider_plugins,
    default_plugins=default_plugins,
)
```

- [ ] **Step 4: Run runtime tests**

Run: `python3 -m pytest tests/test_codex_runtime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit runtime wiring**

Run: `git add codex_runtime.py tests/test_codex_runtime.py && git commit -m "feat: apply Codex default plugins to runtimes"`

### Task 5: Add an isolated real-CLI acceptance test

**Files:**
- Modify: `tests/test_codex_plugin_integration.py`

- [ ] **Step 1: Add the acceptance test**

Use the existing isolated Codex fixture and `use_codex_runtime` helper. Install fixture manifests for the eight IDs (or reuse the curated fixture for Superpowers and create seven local manifests), create a provider whose config has no `[plugins]` table, invoke `codex plugin list --json`, and assert every default ID is reported as installed/enabled in the generated runtime. Hash the CC Switch SQLite file before and after and assert the hashes match.

- [ ] **Step 2: Run the isolated test**

Run: `python3 -m pytest tests/test_codex_plugin_integration.py -q`

Expected: PASS when the installed Codex CLI supports plugins; otherwise the existing test's skip behavior remains intact.

- [ ] **Step 3: Commit the acceptance coverage**

Run: `git add tests/test_codex_plugin_integration.py && git commit -m "test: verify Codex defaults with real CLI"`

### Task 6: Document configuration and verify the complete feature

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the default file and precedence**

Update the Codex plugin section to state that `codex_plugin_defaults.toml` is a one-time eight-plugin snapshot, that only installed entries are enabled, and that it is not synchronized with anyrouter. State the exact precedence: installed defaults, provider config, enabled Common Config, then provider `/plugins` sidecar. Explain that global uninstall suppresses the default until reinstall, without auto-download or warnings, and that editing the file is the way to change the stable defaults.

- [ ] **Step 2: Run documentation and repository checks**

Run: `rg -n 'TB[D]|TO[DO]' docs/superpowers/plans/2026-08-05-codex-default-plugin-settings.md` (expected: no output), `git diff --check`, and `python3 -m compileall -q codex_runtime.py codex_plugin_state.py tests`.

- [ ] **Step 3: Run the complete test suite**

Run: `python3 -m pytest -q`. Expected: all tests pass, with only pre-existing environment-dependent skips.

- [ ] **Step 4: Perform the real launcher and database read-only check**

Run a new provider without plugin declarations through `python3 codex_tui.py --cc-switch-config <provider> -- plugin list --json`, verify the eight default IDs are enabled when installed, then hash the CC Switch database before and after. Do not invoke any CC Switch write command.

- [ ] **Step 5: Commit documentation and final verification**

Run: `git add README.md && git commit -m "docs: explain Codex default plugin settings"`, then run `git status --short` and `git log --oneline -8` to confirm a clean `main` branch with the feature commits.

## Self-review

- Spec coverage: Tasks 1-2 cover the fixed snapshot and every file validation failure; Task 3 covers inventory filtering, precedence, and baseline semantics; Task 4 wires runtime behavior without CC Switch selection coupling; Task 5 covers the real CLI and database immutability; Task 6 documents and verifies the complete workflow.
- Placeholder scan: the required red-flag search returns no matches, and every code change has concrete content.
- Type consistency: `default_plugins` is an optional `Mapping[str, bool]` appended to `compose_effective_config`; `_load_default_plugin_config` returns the same mapping shape and runtime passes it by keyword.
