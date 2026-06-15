# Create Branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a batch "create new branch" workflow that creates one branch name across multiple repositories from the remote main branch, with optional remote push.

**Architecture:** Extend the existing single-file Python Web engine with project-list parsing, `create_branch_one`, SSE streaming, and UI controls. Add a matching `--create <branch> [--push]` mode to the independent bash CLI. Keep the old sync flow unchanged.

**Tech Stack:** Python 3 standard library, bash 3.2-compatible shell, Git CLI, temporary local Git repositories for tests.

---

### Task 1: Python Engine Tests

**Files:**
- Create: `tests/test_create_branches.py`
- Modify: none

- [ ] **Step 1: Write failing tests**

Create `tests/test_create_branches.py` with unittest tests that import `sync-branches-ui.py` via `importlib`, build temporary bare remotes and working clones, then call `parse_project_list` and `create_branch_one`.

Core assertions:
- `parse_project_list("a\n#x\n\nb\n")` returns `(["a", "b"], [])`.
- `create_branch_one(..., push_remote=False)` creates and checks out the new local branch from `origin/HEAD`, does not push remote.
- Dirty worktree content is stashed before branch creation and not restored to the new branch.
- Existing local or remote branch returns `exists`.
- `push_remote=True` pushes the new remote branch and sets upstream.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_create_branches -v`

Expected: failure because `parse_project_list` and `create_branch_one` do not exist yet.

### Task 2: Python Engine Implementation

**Files:**
- Modify: `sync-branches-ui.py`
- Test: `tests/test_create_branches.py`

- [ ] **Step 1: Implement project-list parsing**

Add `parse_project_list(text)` near `parse_entries`. It trims lines, ignores empty lines and `#` comments, and returns `(projects, errors)`.

- [ ] **Step 2: Implement create_branch_one**

Add `create_branch_one(proj, branch, base, push_remote, emit)` near `sync_one`. It validates repo state, stashes dirty worktrees with `sync-branches-create: <orig>`, fetches origin, detects main, skips existing local/remote branch, checks out `branch` from `origin/<main>`, optionally pushes with `git push -u origin <branch>`, and emits `log` plus `result`.

- [ ] **Step 3: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_create_branches -v`

Expected: all tests pass.

### Task 3: Python Web API And UI

**Files:**
- Modify: `sync-branches-ui.py`
- Test: `tests/test_create_branches.py`

- [ ] **Step 1: Add SSE API**

Add `/api/create_branch` to the handler, parse `projects`, `branch`, `base`, `push`, emit parsed entries, and run project workers in parallel like `/api/sync`.

- [ ] **Step 2: Add Web controls**

Add a "创建新分支" section to the embedded `PAGE`: project textarea, branch input, push checkbox, create button, and result cards/logs using the existing card helpers.

- [ ] **Step 3: Smoke test syntax and unit tests**

Run:
- `python3 -m py_compile sync-branches-ui.py`
- `python3 -m unittest tests.test_create_branches -v`

Expected: both commands succeed.

### Task 4: Bash CLI Mode

**Files:**
- Modify: `sync-branches.sh`
- Test: `tests/test_create_branches_cli.sh`

- [ ] **Step 1: Write failing shell integration test**

Create `tests/test_create_branches_cli.sh`. It builds temporary repos, runs `SYNC_BASE_DIR=<tmp>/work ./sync-branches.sh --create dev_cli --push` with project names on stdin, and checks the local branch, remote branch, upstream, and stash behavior.

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_create_branches_cli.sh`

Expected: failure because `--create` mode does not exist yet.

- [ ] **Step 3: Implement bash create mode**

Parse `--create <branch>` and optional `--push` before existing sync input processing. Add a `process_create_project` function that mirrors the Python flow using bash 3.2-compatible syntax and exits through a summary.

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_create_branches_cli.sh`

Expected: shell integration test passes.

### Task 5: Docs And Final Verification

**Files:**
- Modify: `README.md`
- Modify: `dist/README.md`

- [ ] **Step 1: Update docs**

Document the create-branch UI and CLI mode, including the stash-not-restored behavior and optional `--push`.

- [ ] **Step 2: Run verification**

Run:
- `python3 -m py_compile sync-branches-ui.py`
- `python3 -m unittest tests.test_create_branches -v`
- `bash tests/test_create_branches_cli.sh`
- `bash -n sync-branches.sh`
- `git diff --check`

Expected: all commands exit 0.

