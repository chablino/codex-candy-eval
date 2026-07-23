# Codex Originator Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all three Codex evaluators default `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` to `codex-tui` while preserving an explicitly supplied value.

**Architecture:** Each standalone Codex script constructs a fresh child-process environment at its existing `subprocess.run` boundary. It copies either the supplied CC Switch runtime environment or `os.environ`, then uses `setdefault` so explicit values win and neither source mapping is mutated.

**Tech Stack:** Python 3 standard library (`os`, `subprocess`, `unittest.mock`), existing `unittest` suite, Git worktree.

---

## File Map

- Modify: `tests/test_eval_scripts.py` - child environment, precedence, inheritance, mutation, and Claude isolation tests.
- Modify: `codex_candy_eval.py` - prepare the candy evaluator's Codex child environment.
- Modify: `codex_tps_eval.py` - prepare the TPS evaluator's Codex child environment.
- Modify: `codex_juice_eval.py` - import `os` and prepare the juice evaluator's Codex child environment.
- Leave unchanged: `claude_candy_eval.py`, `opencode_candy_eval.py`, `cc_switch_config.py`, prompts, scoring, and CLI arguments.

### Task 1: Specify Child Environment Behavior

**Files:**
- Modify: `tests/test_eval_scripts.py`

- [ ] **Step 1: Replace Codex subprocess identity assertions with copy/default assertions**

For each of `CodexCandyTests`, `CodexTpsTests`, and `CodexJuiceTests`, inspect the mocked `subprocess.run(..., env=...)` mapping and assert:

```python
child_environment = run.call_args.kwargs["env"]
self.assertIsNot(child_environment, environment)
self.assertEqual(child_environment["CODEX_HOME"], "/private/runtime")
self.assertEqual(
    child_environment["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"], "codex-tui"
)
self.assertEqual(environment, {"CODEX_HOME": "/private/runtime"})
```

- [ ] **Step 2: Add explicit-value and inherited-parent tests**

Pass this mapping to each Codex subprocess function and assert the copied child environment preserves it:

```python
environment = {
    "CODEX_HOME": "/private/runtime",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "custom-client",
}
```

Also patch `os.environ` to `{"CODEX_PARENT_ENV": "inherited"}`, call without an `environment` argument, and assert both `CODEX_PARENT_ENV=inherited` and the `codex-tui` default are present in the mocked child environment.

- [ ] **Step 3: Lock Claude behavior**

Keep the Claude subprocess environment as the exact supplied object and assert `CODEX_INTERNAL_ORIGINATOR_OVERRIDE` is absent.

- [ ] **Step 4: Run the focused suite and verify RED**

Run:

```sh
python3 -m unittest tests.test_eval_scripts -v
```

Expected: the nine new Codex environment assertions fail because the current implementation passes the supplied mapping unchanged or passes `env=None`; all Claude tests pass.

### Task 2: Prepare Environments at the Three Codex Boundaries

**Files:**
- Modify: `codex_candy_eval.py`
- Modify: `codex_tps_eval.py`
- Modify: `codex_juice_eval.py`

- [ ] **Step 1: Add the minimal environment preparation**

Immediately before each Codex `subprocess.run`, construct and default the child environment:

```python
child_environment = dict(environment) if environment is not None else os.environ.copy()
child_environment.setdefault("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex-tui")
```

Pass `env=child_environment`. Add `import os` to `codex_juice_eval.py`; the other two scripts already import it.

- [ ] **Step 2: Run the focused suite and verify GREEN**

Run:

```sh
python3 -m unittest tests.test_eval_scripts -v
```

Expected: all 27 tests pass.

- [ ] **Step 3: Review the diff for scope and mutation safety**

Run:

```sh
git diff -- codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py tests/test_eval_scripts.py
```

Confirm the source mapping is copied before `setdefault`, only Codex scripts changed, and no prompt or command arguments changed.

### Task 3: Verify and Commit

**Files:**
- Verify all changed Python and documentation files.

- [ ] **Step 1: Run complete automated verification**

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile cc_switch_config.py codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py claude_candy_eval.py
git diff --check
```

Expected: all tests pass, compilation exits zero, and `git diff --check` emits no output.

- [ ] **Step 2: Inspect final repository state**

```sh
git status --short --branch
git diff --stat
```

Expected: only the three Codex scripts, `tests/test_eval_scripts.py`, and this plan/design documentation differ from `main` through the feature branch commits.

- [ ] **Step 3: Commit the implementation**

```sh
git add codex_candy_eval.py codex_tps_eval.py codex_juice_eval.py tests/test_eval_scripts.py
git commit -m "fix: default Codex evaluator originator"
```
