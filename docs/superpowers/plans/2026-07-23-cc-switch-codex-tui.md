# CC Switch Codex TUI Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an interactive `codex_tui.py` launcher that runs Codex with one selected CC Switch Codex provider while sharing the user's existing Codex sessions and state.

**Architecture:** Reuse `load_provider("codex", selector)` for a read-only provider snapshot. Materialize only the provider's TOML as a random owner-only profile inside the existing `CODEX_HOME`, pass provider auth through a copied child environment, and remove the profile when the child exits. The launcher owns only its selector and forwards arguments after `--` to `codex`.

**Tech Stack:** Python 3 standard library, `unittest`, SQLite read-only access, Codex CLI profiles.

---

### Task 1: Codex Profile Runtime Contract

**Files:**
- Modify: `cc_switch_config.py` after `ProviderRuntime` and near `materialize_provider`
- Test: `tests/test_cc_switch_config.py` in a new `CodexProfileRuntimeTests` class

- [ ] **Step 1: Write failing tests for shared-home profile materialization**

Add tests that create an existing temporary `CODEX_HOME`, construct a `SelectedProvider`, and assert that `materialize_codex_profile` writes a unique `cc-switch-*.config.toml` with the exact provider config and mode `0600`, preserves `CODEX_HOME`, `CODEX_SQLITE_HOME`, and inherited variables, injects string auth values into only the returned environment, defaults `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` to `codex-tui`, preserves an explicit originator, and leaves the parent environment unchanged.

Also cover cleanup on normal body exit, body exception, and SIGTERM through `use_codex_profile`; two live contexts must get different profile names; invalid auth keys/values, missing `CODEX_HOME`, a non-directory home, and cleanup failure must raise sanitized `CcSwitchConfigError` without exposing secrets.

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
python3 -m unittest tests.test_cc_switch_config.CodexProfileRuntimeTests -v
```

Expected: import or attribute failures because `CodexProfileRuntime`, `materialize_codex_profile`, and `use_codex_profile` do not exist yet.

- [ ] **Step 3: Implement the minimal profile runtime**

Add:

```python
@dataclass(frozen=True)
class CodexProfileRuntime:
    provider_id: str
    provider_name: str
    profile_name: str
    environment: Mapping[str, str] = field(repr=False)
```

Implement a helper that resolves `CODEX_HOME` from the inherited environment or `Path.home() / ".codex"`, requires an existing directory, and writes `cc-switch-<uuid4().hex>.config.toml` with `_write_private_file`. Validate every `auth` key and value as strings, reject empty/NUL-containing/invalid environment keys without echoing values, copy `os.environ`, set auth variables, and use `setdefault` for `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`. Keep `CODEX_HOME` and `CODEX_SQLITE_HOME` unchanged in the child environment. Implement `materialize_codex_profile(provider)` as a context manager that deletes only the profile, preserves body exceptions, and reports cleanup/setup errors as sanitized `CcSwitchConfigError`. Implement `use_codex_profile(selector, db_path=DEFAULT_DB_PATH)` with the existing macOS/Linux check, `load_provider("codex", ...)`, and `_sigterm_as_system_exit` surrounding materialization.

- [ ] **Step 4: Run the focused and full existing tests**

Run:

```bash
python3 -m unittest tests.test_cc_switch_config.CodexProfileRuntimeTests -v
python3 -m unittest discover -s tests -v
```

Expected: all new profile tests and all pre-existing tests pass.

- [ ] **Step 5: Commit the runtime**

```bash
git add cc_switch_config.py tests/test_cc_switch_config.py
git commit -m "feat: add shared Codex profile runtime"
```

### Task 2: Interactive TUI Launcher

**Files:**
- Create: `codex_tui.py`
- Create: `tests/test_codex_tui.py`

- [ ] **Step 1: Write failing launcher tests**

Test parser behavior for the required `--cc-switch-config`, separator removal, empty and non-empty Codex remainder arguments, and rejection of forwarded `--profile`, `--profile=NAME`, `-p`, and `-pNAME`. Test `main()` with mocked `use_codex_profile`, `shutil.which`, and `subprocess.run`: it must call the runtime with selector, run `[codex_executable, "--profile", runtime.profile_name, *args]`, pass `env=runtime.environment`, omit stream capture so terminal streams are inherited, and return the child's exit code. Test setup errors and missing Codex return `2`, while a child exit code is preserved. Test forwarded `resume`, session ID, and `--model` order.

- [ ] **Step 2: Run launcher tests to verify RED**

Run:

```bash
python3 -m unittest tests.test_codex_tui -v
```

Expected: module/import failures because `codex_tui.py` does not exist.

- [ ] **Step 3: Implement the launcher**

Use `argparse` with a required `--cc-switch-config` and `argparse.REMAINDER` for Codex arguments. Strip one leading `--` from the remainder. Reject profile options before provider materialization. Resolve `codex` with `shutil.which`; print a concise sanitized error and return `2` when unavailable or when `CcSwitchConfigError` is raised. Within `use_codex_profile`, call `subprocess.run([executable, "--profile", runtime.profile_name, *codex_args], env=runtime.environment)` without `capture_output`, `stdout`, `stderr`, or `stdin` overrides. Return `proc.returncode`. Add a `main(argv=None)` and `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: Run launcher and full tests**

Run:

```bash
python3 -m unittest tests.test_codex_tui -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass without starting a real Codex process.

- [ ] **Step 5: Commit the launcher**

```bash
git add codex_tui.py tests/test_codex_tui.py
git commit -m "feat: add CC Switch Codex TUI launcher"
```

### Task 3: Documentation and Static Verification

**Files:**
- Modify: `README.md` after the existing evaluator `--cc-switch-config` section

- [ ] **Step 1: Document interactive launch and session sharing**

Add examples for:

```bash
python3 codex_tui.py --cc-switch-config jianzhile
python3 codex_tui.py --cc-switch-config jianzhile -- resume
python3 codex_tui.py --cc-switch-config jianzhile -- resume SESSION_ID --model gpt-5.6-sol
```

Explain that only Codex providers are selected, the App selection is untouched, all providers share the current `CODEX_HOME` sessions/SQLite/history/skills/plugins, auth is injected only into the child process, and the temporary profile is removed after exit. State that forwarded `--profile`/`-p` is reserved by the launcher.

- [ ] **Step 2: Run final verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_tui.py codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
```

Expected: zero test failures, successful compilation, and no whitespace errors. Confirm the worktree contains only the planned implementation/docs changes before committing.

- [ ] **Step 3: Commit documentation and verification-ready implementation**

```bash
git add README.md
git commit -m "docs: document CC Switch Codex TUI usage"
```

- [ ] **Step 4: Report integration choices**

Show the final branch, commits, test result, and the safe commands for merging/pushing this feature branch. Do not delete worktrees or push without an explicit user request.
