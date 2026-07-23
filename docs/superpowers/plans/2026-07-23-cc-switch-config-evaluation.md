# CC Switch Evaluation Configs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Add a secure per-process --cc-switch-config NAME_OR_ID selector to the three Codex evaluators and the Claude candy evaluator without changing the CC Switch App's current provider.

**Architecture:** A new read-only cc_switch_config.py owns provider lookup, validation, private runtime files, signal-safe cleanup, and redaction. Each evaluator lazily imports it only when a selector is supplied, then passes the runtime environment and redactor through its existing subprocess function. The no-selector path remains self-contained for local use and curl/wget pipe mode.

**Tech Stack:** Python 3 standard library (sqlite3, json, tempfile, subprocess, signal, unittest), POSIX file permissions, existing argparse/table renderers, no third-party dependencies.

---

## File Map

- Create: cc_switch_config.py — read-only selection, validation, private runtime, lifecycle, and redaction.
- Create: tests/test_cc_switch_config.py — temporary SQLite and runtime security tests.
- Create: tests/test_eval_scripts.py — parser, environment, startup, redaction, and no-selector tests for all four evaluators.
- Modify: codex_candy_eval.py, codex_tps_eval.py, codex_juice_eval.py, claude_candy_eval.py.
- Modify: README.md.
- Leave unchanged: opencode_candy_eval.py, example.png, prompt text, table formatting, and scoring rules.

Use a clean feature branch/worktree before Task 1. Do not import code at runtime from anyrouter-keeper.

## Shared Test Fixture

Create this fixture in tests/test_cc_switch_config.py:

~~~python
class ProviderDatabase:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "cc-switch.db"

    def create_schema(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE providers (
                    id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_config TEXT NOT NULL
                )
                """
            )

    def insert(self, provider_id, app_type, name, settings):
        payload = settings if isinstance(settings, str) else json.dumps(settings)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO providers (id, app_type, name, settings_config) VALUES (?, ?, ?, ?)",
                (provider_id, app_type, name, payload),
            )

    def close(self):
        self.directory.cleanup()


CODEX_SETTINGS = {
    "config": 'model = "gpt-test"\n',
    "auth": {"OPENAI_API_KEY": "codex-test-secret"},
}
CLAUDE_SETTINGS = {
    "env": {"ANTHROPIC_AUTH_TOKEN": "claude-test-secret"},
    "model": "sonnet",
}
~~~

## Task 1: Read-Only Provider Selection

**Files:**
- Create: cc_switch_config.py
- Create: tests/test_cc_switch_config.py

- [ ] **Step 1: Write failing selector tests**

Test exact ID precedence, unique name selection scoped by app_type, duplicate-name IDs, missing-name listings without payload, missing database/table/column errors, invalid JSON, non-standard JSON constants, Codex config/auth shape, and Claude env shape. At minimum include:

~~~python
def test_id_wins_over_name(self):
    self.database.insert("target", "codex", "first", CODEX_SETTINGS)
    self.database.insert("other", "codex", "target", CODEX_SETTINGS)
    selected = load_provider("codex", "target", self.database.path)
    self.assertEqual((selected.provider_id, selected.name), ("target", "first"))

def test_same_name_is_scoped_to_app_type(self):
    self.database.insert("codex-id", "codex", "anyrouter", CODEX_SETTINGS)
    self.database.insert("claude-id", "claude", "anyrouter", CLAUDE_SETTINGS)
    self.assertEqual(
        load_provider("codex", "anyrouter", self.database.path).provider_id,
        "codex-id",
    )

def test_duplicate_name_lists_ids_without_settings(self):
    self.database.insert("first", "codex", "same", CODEX_SETTINGS)
    self.database.insert("second", "codex", "same", CODEX_SETTINGS)
    with self.assertRaises(CcSwitchConfigError) as raised:
        load_provider("codex", "same", self.database.path)
    self.assertIn("first", str(raised.exception))
    self.assertIn("second", str(raised.exception))
    self.assertNotIn("codex-test-secret", str(raised.exception))
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~sh
python3 -m unittest discover -s tests -p 'test_cc_switch_config.py' -v
~~~

Expected: import failure because cc_switch_config.py is absent.

- [ ] **Step 3: Implement the loader**

Create CcSwitchConfigError, SelectedProvider(provider_id, app_type, name, settings), and load_provider(app_type, selector, db_path=DEFAULT_DB_PATH). The implementation must:

1. Resolve and verify the database path.
2. Open f"{path.as_uri()}?mode=ro" with sqlite3.connect(..., uri=True).
3. Validate providers and the four required columns.
4. Query only id,name first, filtered by app_type, sorted by name,id.
5. Match exact ID first, then exact name; never query settings_config for missing or duplicate selectors.
6. Query the selected payload in a second read-only connection, verify exactly one row, and close both connections in finally.
7. Parse with json.loads(..., parse_constant=reject_constant); require an object, Codex string config plus object auth, and Claude object env when present.
8. Raise payload-free CcSwitchConfigError messages. Translate malformed JSON with from None so the original exception chain is not exposed.

- [ ] **Step 4: Run selector tests and compile**

~~~sh
python3 -m unittest discover -s tests -p 'test_cc_switch_config.py' -v
python3 -m py_compile cc_switch_config.py
~~~

Expected: all selector tests pass and compilation exits 0.

- [ ] **Step 5: Commit**

~~~sh
git add cc_switch_config.py tests/test_cc_switch_config.py
git commit -m "feat: load CC Switch providers read-only"
~~~

## Task 2: Private Runtime, Environment Snapshot, Signals, and Redaction

**Files:**
- Modify: cc_switch_config.py
- Modify: tests/test_cc_switch_config.py

- [ ] **Step 1: Write failing runtime/security tests**

Cover Codex and Claude files, 0700/0600 modes, inherited environment without parent mutation, cleanup after body exceptions, cleanup-only exception-chain sanitization, descriptor closure on fchmod/write failure, Windows selector rejection, SIGTERM handler restoration, and overlap/multiline/truncation redaction. Representative tests:

~~~python
def test_codex_runtime_files_modes_and_environment(self):
    provider = SelectedProvider("id", "codex", "anyrouter", CODEX_SETTINGS)
    with mock.patch.dict(os.environ, {"CC_SWITCH_TEST_BASE": "inherited"}, clear=True):
        with materialize_provider(provider) as runtime:
            root = Path(runtime.environment["CODEX_HOME"])
            self.assertEqual((root / "config.toml").read_text(), CODEX_SETTINGS["config"])
            self.assertEqual(json.loads((root / "auth.json").read_text()), CODEX_SETTINGS["auth"])
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "auth.json").stat().st_mode), 0o600)
            self.assertEqual(runtime.environment["CC_SWITCH_TEST_BASE"], "inherited")
            self.assertIn("codex-test-secret", runtime.secrets)
        self.assertFalse(root.exists())
        self.assertEqual(dict(os.environ), {"CC_SWITCH_TEST_BASE": "inherited"})

def test_redaction_handles_overlap_and_multiline(self):
    self.assertEqual(redact_text("x acted y", ("acted",)), "x <redacted> y")
    self.assertEqual(
        redact_text("before line-one\nline-two after", (" line-one\nline-two ",)),
        "before<redacted>after",
    )
~~~

- [ ] **Step 2: Run tests and verify RED**

~~~sh
python3 -m unittest discover -s tests -p 'test_cc_switch_config.py' -v
~~~

Expected: runtime API tests fail because ProviderRuntime and materialize_provider are absent.

- [ ] **Step 3: Implement the runtime API**

Add:

~~~python
@dataclass(frozen=True)
class ProviderRuntime:
    provider_id: str
    provider_name: str
    environment: Mapping[str, str] = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)

    def redact(self, text: str) -> str:
        return redact_text(text, self.secrets)
~~~

Implement _write_private_file with os.open(..., O_EXCL, 0o600), os.fchmod, os.fdopen, and a finally descriptor close. Recursively collect string leaves from Codex auth or Claude env, deduplicate, and sort longest-first.

Implement redact_text(text, secrets: Sequence[str]) with one escaped regex alternation and callable replacement; never use sequential replace calls.

Implement materialize_provider(provider) as a context manager that creates a unique TemporaryDirectory, chmods the root 0700, writes Codex config.toml/auth.json or Claude settings.json, copies os.environ and updates only the provider home key, yields ProviderRuntime, and cleans in finally. Preserve a caller BaseException if cleanup also fails; translate setup and cleanup-only failures to sanitized CcSwitchConfigError.

Implement _sigterm_as_system_exit and use_provider(app_type, selector, db_path=DEFAULT_DB_PATH). use_provider must reject os.name == "nt" before database access, load once, and nest materialization and SIGTERM contexts so SystemExit(128 + SIGTERM) cleans the runtime.

- [ ] **Step 4: Run runtime, full, and compile checks**

~~~sh
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py
git diff --check
~~~

- [ ] **Step 5: Commit**

~~~sh
git add cc_switch_config.py tests/test_cc_switch_config.py
git commit -m "feat: materialize isolated CC Switch runtimes"
~~~

## Task 3: Codex Candy Integration

**Files:**
- Modify: codex_candy_eval.py
- Modify: tests/test_eval_scripts.py

- [ ] **Step 1: Add failing tests**

Test parser acceptance, run_codex environment and redaction, one provider-context entry for -n 2, and no selector avoiding load_provider. Use FakeRuntime(environment, secrets) with redact and a patched cc_switch_config.use_provider context that asserts app_type == "codex".

~~~python
def test_codex_parser_accepts_selector(self):
    args = codex_candy_eval.parse_args(
        ["--cc-switch-config", "anyrouter", "-m", "gpt-test", "-n", "2"]
    )
    self.assertEqual((args.cc_switch_config, args.model, args.tests),
                     ("anyrouter", "gpt-test", 2))

def test_codex_subprocess_receives_runtime_and_redacts_error(self):
    with mock.patch.object(codex_candy_eval, "resolve_codex_executable", return_value="codex"):
        with mock.patch.object(
            codex_candy_eval.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "secret-token"),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "<redacted>"):
                codex_candy_eval.run_codex(
                    None, "medium",
                    environment={"CODEX_HOME": "/private/runtime"},
                    redact=lambda text: text.replace("secret-token", "<redacted>"),
                )
    self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "/private/runtime")
~~~

- [ ] **Step 2: Run tests and verify RED**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
~~~

Expected: parse_args rejects the option and run_codex has no runtime parameters.

- [ ] **Step 3: Refactor parser and subprocess boundary**

Extract parse_args(argv=None), add --cc-switch-config NAME_OR_ID, and change run_codex to accept environment=None and redact=None. Pass env=environment to subprocess.run. On nonzero exit, form the existing stderr/stdout message, call redact when present, then raise RuntimeError.

- [ ] **Step 4: Add lazy startup orchestration**

Move the existing table loop into _evaluate(args, use_ansi, runtime); every run_codex call receives runtime.environment and runtime.redact when runtime exists, otherwise None.

Use this startup shape:

~~~python
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
        print("ERROR: --cc-switch-config requires the complete repository", file=sys.stderr)
        return 2
    try:
        with use_provider("codex", args.cc_switch_config) as runtime:
            _evaluate(args, use_ansi, runtime)
    except CcSwitchConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0
~~~

Use raise SystemExit(main()) at module entry. Do not catch SystemExit.

- [ ] **Step 5: Run and commit**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
python3 codex_candy_eval.py --help
python3 -m py_compile codex_candy_eval.py
git diff --check
git add codex_candy_eval.py tests/test_eval_scripts.py
git commit -m "feat: select CC Switch config for Codex candy eval"
~~~

## Task 4: Claude Candy Integration

**Files:**
- Modify: claude_candy_eval.py
- Modify: tests/test_eval_scripts.py

- [ ] **Step 1: Add failing tests**

Add parser, provider-type, one-context-for-two-runs, CLAUDE_CONFIG_DIR subprocess, no-selector, redacted stderr, and redacted JSON is_error.result tests. Assert use_provider receives claude and every run_claude call receives the same runtime environment.

- [ ] **Step 2: Run RED**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
~~~

Expected: Claude parser and runtime-injection assertions fail.

- [ ] **Step 3: Implement**

Extract parse_args(argv=None), add the option, extend run_claude(model, effort, environment=None, redact=None), pass env=environment to subprocess.run, redact nonzero stderr before raising, and also redact data.result/data.subtype before raising when the JSON response has is_error set. Move the existing table loop into an evaluator function. Use use_provider("claude", ...) and preserve JSON usage fallback and output columns.

- [ ] **Step 4: Verify and commit**

~~~sh
python3 -m unittest discover -s tests -v
python3 claude_candy_eval.py --help
python3 -m py_compile claude_candy_eval.py
git diff --check
git add claude_candy_eval.py tests/test_eval_scripts.py
git commit -m "feat: select CC Switch config for Claude candy eval"
~~~

## Task 5: Codex TPS Integration

**Files:**
- Modify: codex_tps_eval.py
- Modify: tests/test_eval_scripts.py

- [ ] **Step 1: Add failing tests**

Test selector/model parsing, run_codex(prompt, model, effort, environment, redact), and a fake-context main. Patch run_codex to return a valid tuple and assert exactly five calls, one per QUESTIONS item, one context entry with app_type codex, and the same environment/redactor for every call.

- [ ] **Step 2: Run RED**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
~~~

Expected: selector parsing and runtime injection fail.

- [ ] **Step 3: Implement**

Extract parse_args(argv=None), add the option, extend run_codex with environment and redact, pass env=environment, redact nonzero stderr before raising, and pass one runtime through all five question calls. Preserve QUESTIONS, JSON parsing, table columns, and TPS calculation. Use use_provider("codex", ...) and return 2 for missing-module/config errors.

- [ ] **Step 4: Verify and commit**

~~~sh
python3 -m unittest discover -s tests -v
python3 codex_tps_eval.py --help
python3 -m py_compile codex_tps_eval.py
git diff --check
git add codex_tps_eval.py tests/test_eval_scripts.py
git commit -m "feat: select CC Switch config for TPS eval"
~~~

## Task 6: Codex Juice Integration

**Files:**
- Modify: codex_juice_eval.py
- Modify: tests/test_eval_scripts.py

- [ ] **Step 1: Add failing tests**

Test selector/model parsing, ask(model, effort, environment, redact), and a fake-context main using a model with a deterministic effort list. Patch ask to return a valid numeric answer and assert every effort call receives the same environment/redactor and the context is entered once.

- [ ] **Step 2: Run RED**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
~~~

Expected: selector parsing and runtime injection fail.

- [ ] **Step 3: Implement**

Extract parse_args(argv=None), add the option, extend ask with environment and redact, pass env=environment to subprocess.run, redact nonzero stderr before raising, and pass one runtime through all model-effort calls. Preserve MODEL_EFFORTS, retries, numeric parsing, and output.

- [ ] **Step 4: Verify and commit**

~~~sh
python3 -m unittest discover -s tests -p 'test_eval_scripts.py' -v
python3 codex_juice_eval.py --help
python3 -m py_compile codex_juice_eval.py
git diff --check
git add codex_juice_eval.py tests/test_eval_scripts.py
git commit -m "feat: select CC Switch config for juice eval"
~~~

## Task 7: README and Final Acceptance

**Files:**
- Modify: README.md
- Verify: all source and test files

- [ ] **Step 1: Document all four commands and boundaries**

Add commands:

~~~sh
python codex_candy_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high -n 5
python codex_tps_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol -r high
python codex_juice_eval.py --cc-switch-config anyrouter -m gpt-5.6-sol
python claude_candy_eval.py --cc-switch-config anyrouter -m sonnet -r high -n 5
~~~

Explain script-based provider scoping, ID disambiguation, startup snapshot, no App switching, no cc-switch CLI, POSIX-only selector, and full-repository requirement for selector mode in a pipe.

- [ ] **Step 2: Run complete automated checks**

~~~sh
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
git status --short
~~~

Expected: all tests pass, compilation succeeds, diff check is empty, and no generated credential/config files appear.

- [ ] **Step 3: Run no-request smoke checks against the real App database**

Run a direct materialize/cleanup check, not an evaluator request:

~~~sh
python3 <<'PY'
from pathlib import Path

from cc_switch_config import use_provider

for app in ("codex", "claude"):
    with use_provider(app, "anyrouter") as runtime:
        key = "CODEX_HOME" if app == "codex" else "CLAUDE_CONFIG_DIR"
        print(app, runtime.provider_name, key, Path(runtime.environment[key]).exists())
PY
~~~

Expected: one line per provider with the expected temporary path existing inside the context; the context then removes it. Do not run a real model request as automated verification.

- [ ] **Step 4: Inspect credentials and commit README**

Run:

~~~sh
rg -n 'sk-|OPENAI_API_KEY|ANTHROPIC_AUTH_TOKEN' .
rg --files | rg '(^|/)(config.toml|auth.json|settings.json|cc-switch.db)$|codex-candy-eval-'
~~~

Only test fixture labels may match the first command; the second must find no generated runtime files. Then:

~~~sh
git add README.md
git commit -m "docs: explain CC Switch evaluation configs"
~~~

- [ ] **Step 5: Final clean-state check**

~~~sh
git status --short --branch
git log --oneline --decorate -8
~~~

Expected: clean worktree, focused commits, and no real API request made by automated checks.
