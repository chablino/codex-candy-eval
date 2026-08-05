# Codex Plugin Marketplace Auth Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The user explicitly requested inline execution without subagents.

**Goal:** Make isolated Codex runtimes use the same API-key curated marketplace as normal Codex launches, while ignoring stale full-catalog cache entries.

**Architecture:** Keep provider model routing environment-based, but materialize the selected provider key into a private, temporary `auth.json` so Codex selects `openai-api-curated`. Treat `openai-curated` as a different built-in marketplace identity and exclude its entire cache from launcher inventory.

**Tech Stack:** Python 3 standard library, `unittest`, TOML via `tomllib`/`tomli-w`, real Codex CLI integration checks.

---

### Task 1: Materialize Ephemeral API-Key Authentication

**Files:**
- Modify: `tests/test_codex_runtime.py:103`
- Modify: `codex_runtime.py:151-182`
- Modify: `codex_runtime.py:303-323`

- [ ] **Step 1: Change the isolation test to require a private temporary auth file**

Replace the assertion that `auth.json` is absent with assertions that the temporary file is regular, private, selected-provider-specific, and does not change the shared auth file:

```python
auth_path = runtime_home / "auth.json"
self.assertTrue(auth_path.is_file())
self.assertFalse(auth_path.is_symlink())
self.assertEqual(
    json.loads(auth_path.read_text(encoding="utf-8")),
    {"OPENAI_API_KEY": "provider-secret"},
)
self.assertEqual(stat.S_IMODE(auth_path.stat().st_mode), 0o600)
```

Keep the existing post-context assertions that the runtime home is deleted and the shared `auth.json` remains unchanged.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest tests.test_codex_runtime.CodexRuntimeMaterializationTests.test_complete_runtime_is_isolated_and_shares_only_allowlist -v
```

Expected: FAIL because `<runtime_home>/auth.json` does not exist.

- [ ] **Step 3: Return one validated auth payload from `_codex_auth_parts`**

Change the return type and result so the transformed config, child environment, and private auth document all derive from the same validated key:

```python
def _codex_auth_parts(
    document: Mapping[str, Any], auth: object
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    # existing validation and provider transformation remain unchanged
    api_key = auth["OPENAI_API_KEY"]
    return (
        transformed,
        {"OPENAI_API_KEY": api_key},
        {"OPENAI_API_KEY": api_key},
    )
```

The environment and file dictionaries are deliberately separate; no key may enter `config.toml` or object reprs.

- [ ] **Step 4: Write `auth.json` with the existing private-file helper**

At runtime preparation, serialize the validated auth document and write it beside `config.toml`:

```python
final_document, auth_environment, auth_document = _codex_auth_parts(
    composed.document, provider.settings.get("auth")
)
rendered_auth = json.dumps(auth_document, separators=(",", ":")) + "\n"

config_path = runtime_home / "config.toml"
_write_private_file(config_path, rendered)
_write_private_file(runtime_home / "auth.json", rendered_auth)
```

Add the standard-library `json` import. Existing setup error handling must delete the partial temporary home and sanitize the underlying write error.

- [ ] **Step 5: Run the focused materialization tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_codex_runtime.CodexRuntimeMaterializationTests -v
```

Expected: all materialization tests PASS.

- [ ] **Step 6: Commit the temporary-auth behavior**

```bash
git add -- codex_runtime.py tests/test_codex_runtime.py
git commit -m "fix: materialize provider auth in Codex runtime"
```

### Task 2: Exclude the Full Curated Cache from API Inventory

**Files:**
- Modify: `tests/test_codex_plugin_state.py:214-222`
- Modify: `codex_plugin_state.py:190-225`

- [ ] **Step 1: Expand the inventory regression test**

Rename the legacy-Superpowers-only test and add another valid plugin under `openai-curated`:

```python
def test_full_curated_cache_is_ignored_and_api_curated_is_discovered(self):
    self.install("openai-curated", "superpowers")
    self.install("openai-curated", "figma")
    self.install("openai-api-curated", "superpowers")

    self.assertEqual(
        scan_plugin_inventory(self.plugins_root),
        frozenset({CANONICAL_SUPERPOWERS_ID}),
    )
```

- [ ] **Step 2: Run the focused inventory test and verify RED**

Run:

```bash
python3 -m unittest tests.test_codex_plugin_state.PluginInventoryTests.test_full_curated_cache_is_ignored_and_api_curated_is_discovered -v
```

Expected: FAIL because `figma@openai-curated` is currently included.

- [ ] **Step 3: Ignore the complete alternate built-in marketplace**

Skip `openai-curated` before descending into its plugin directories:

```python
for marketplace in _directories(cache):
    if (
        not marketplace.is_dir()
        or marketplace.is_symlink()
        or not _safe_path_segment(marketplace.name)
        or marketplace.name == "openai-curated"
    ):
        continue
```

Remove the narrower special case that only skipped Superpowers. Preserve all manifest and path validation for every other marketplace.

- [ ] **Step 4: Run plugin state tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_codex_plugin_state -v
```

Expected: all plugin state tests PASS.

- [ ] **Step 5: Commit inventory filtering**

```bash
git add -- codex_plugin_state.py tests/test_codex_plugin_state.py
git commit -m "fix: ignore alternate curated plugin cache"
```

### Task 3: Verify the Real Codex Marketplace Identity

**Files:**
- Modify: `tests/test_codex_plugin_integration.py:25-78`
- Modify: `tests/test_codex_plugin_integration.py:172-297`

- [ ] **Step 1: Add an isolated managed-curated snapshot fixture**

In `setUp`, call a new helper before creating the CC Switch test database:

```python
self._create_curated_marketplace()
self._create_marketplace()
self._create_database()
```

Implement the fixture without reading or writing the user's Codex home:

```python
def _create_curated_marketplace(self):
    root = self.shared_home / ".tmp" / "plugins"
    marketplace_manifest = root / ".agents" / "plugins" / "marketplace.json"
    marketplace_manifest.parent.mkdir(parents=True)
    marketplace_manifest.write_text(
        json.dumps(
            {
                "name": "openai-curated",
                "interface": {"displayName": "Codex official"},
                "plugins": [
                    {
                        "name": "superpowers",
                        "source": {
                            "source": "local",
                            "path": "./plugins/superpowers",
                        },
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": "Developer Tools",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plugin_root = root / "plugins" / "superpowers"
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True)
    plugin_manifest.write_text(
        json.dumps(
            {
                "name": "superpowers",
                "version": "1.0.0",
                "description": "Isolated API marketplace fixture",
                "skills": "./skills/",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    skill = plugin_root / "skills" / "fixture" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        """\
---
name: fixture
description: Isolated API marketplace fixture.
---

# Fixture
""",
        encoding="utf-8",
    )
```

- [ ] **Step 2: Add a JSON marketplace command helper**

Reuse `_plugin_command` for the real CLI marketplace subcommand by adding a narrowly scoped helper:

```python
def _marketplace_command(self, runtime):
    result = subprocess.run(
        [self.codex, "plugin", "marketplace", "list", "--json"],
        cwd=self.root,
        env=runtime.environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)
```

- [ ] **Step 3: Assert authenticated marketplace identity before the lifecycle flow**

Inside the existing real CLI lifecycle test, before the first plugin command, assert:

```python
self.assertEqual(
    json.loads((runtime.home / "auth.json").read_text()),
    {"OPENAI_API_KEY": "integration-test-secret"},
)
```

Then inspect exact marketplace names:

```python
marketplaces = self._marketplace_command(runtime)
names = {entry["name"] for entry in marketplaces["marketplaces"]}
self.assertIn("openai-api-curated", names)
self.assertNotIn("openai-curated", names)
```

Keep the custom `test-marketplace` lifecycle assertions unchanged.

- [ ] **Step 4: Run the real CLI integration test**

Run:

```bash
python3 -m unittest tests.test_codex_plugin_integration -v
```

Expected: PASS with the installed Codex 0.146.0, using only temporary test homes and marketplaces.

- [ ] **Step 5: Commit integration coverage**

```bash
git add -- tests/test_codex_plugin_integration.py
git commit -m "test: cover Codex API marketplace authentication"
```

### Task 4: Document the Correct Runtime Boundary

**Files:**
- Modify: `README.md:56-83`

- [ ] **Step 1: Update temporary-auth documentation**

Replace the claim that the provider token exists only in the environment with text that states:

```text
provider token is passed through OPENAI_API_KEY and also written to a mode-0600
auth.json inside the unique temporary CODEX_HOME. The file is not shared with
~/.codex and is removed with the temporary runtime.
```

Explain that this keeps direct Codex and launcher sessions on
`superpowers@openai-api-curated`, and that stale `openai-curated` cache entries are not treated as launcher installations.

- [ ] **Step 2: Check documentation formatting**

Run:

```bash
git diff --check -- README.md
```

Expected: exit 0 with no output.

- [ ] **Step 3: Commit documentation**

```bash
git add -- README.md
git commit -m "docs: explain temporary Codex API authentication"
```

### Task 5: Full Verification and Live Launcher Check

**Files:**
- Verify only; no expected production changes.

- [ ] **Step 1: Run the complete automated suite**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run repository checks**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors and no unintended files.

- [ ] **Step 3: Verify a newly materialized real provider runtime**

Run the launcher with a noninteractive plugin command against a provider selected by the existing read-only CC Switch path:

```bash
python3 codex_tui.py --cc-switch-config hlool -- plugin marketplace list --json
```

Expected: the JSON includes `openai-api-curated` and does not include `openai-curated`. Then run:

```bash
python3 codex_tui.py --cc-switch-config hlool -- plugin list --marketplace openai-api-curated --available --json
```

Expected: `superpowers@openai-api-curated` appears under `installed`, not `available`.

This check may update launcher-owned state under `~/.codex/.cc-switch-tui/`, but must not write the CC Switch database. The integration lifecycle test compares database bytes and rows before and after finalization.

- [ ] **Step 4: Leave stale cache cleanup for explicit approval**

Report that `~/.codex/plugins/cache/openai-curated/superpowers` remains untouched. Do not delete it during implementation or verification.
