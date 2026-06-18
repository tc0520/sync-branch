# Switch Branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a batch switch-branch workflow that stashes current work, switches multiple repositories to one target branch, merges remote target updates and remote main updates, and leaves each repository on the target branch.

**Architecture:** Extend the existing Python Web engine with `switch_branch_one`, `sse_switch_branch`, `/api/switch_branch`, and a third `/switch` page. Extend the independent bash CLI with `--switch <branch>`. Reuse existing conflict resolution UI and make `resume_one` mode-aware so switch conflicts finish without pushing, switching back, or popping stash.

**Tech Stack:** Python 3 standard library, bash 3.2-compatible shell, Git CLI, temporary local Git repositories for tests.

---

### Task 1: Python Engine Tests

**Files:**
- Modify: `tests/test_create_branches.py`

- [x] **Step 1: Write failing Python tests**

Add tests that call `switch_branch_one` directly:

```python
def test_switch_branch_stashes_dirty_work_and_merges_main(self):
    clone, _remote, work = make_project(self.root, "repo")
    git(["checkout", "-b", "dev_target"], clone)
    git(["push", "-u", "origin", "dev_target"], clone)
    git(["checkout", "main"], clone)
    (clone / "main.txt").write_text("main advance\n", encoding="utf-8")
    git(["add", "main.txt"], clone)
    git(["commit", "-m", "main advance"], clone)
    git(["push", "origin", "main"], clone)
    (clone / "scratch.txt").write_text("old work\n", encoding="utf-8")
    events, emit = self.collect_events()

    self.mod.switch_branch_one("repo", "dev_target", str(work), emit)

    self.assertEqual("dev_target", git(["branch", "--show-current"], clone))
    self.assertTrue((clone / "main.txt").exists())
    self.assertFalse((clone / "scratch.txt").exists())
    self.assertIn("sync-branches-switch: main", git(["stash", "list"], clone))
    self.assertEqual("ok", self.latest_result(events)["state"])
```

Also add:
- remote-only target branch creates a local tracking branch and switches to it.
- missing target branch returns `error`.
- switch conflict returns `conflict` with `resume=True`.
- switch conflict resume stays on target and does not push or pop stash.
- `list_stashes` and `pop_stash` recognize `sync-branches-switch`.

- [x] **Step 2: Verify tests fail**

Run: `python3 -m unittest tests.test_create_branches -v`

Expected: errors because `switch_branch_one` does not exist.

### Task 2: Python Engine Implementation

**Files:**
- Modify: `sync-branches-ui.py`
- Test: `tests/test_create_branches.py`

- [x] **Step 1: Implement `switch_branch_one`**

Add a function near `create_branch_one` that:
- validates repo and branch state,
- stashes dirty work as `sync-branches-switch: <orig>`,
- fetches origin,
- detects main branch,
- checks out local target or creates from `origin/<target>`,
- merges `origin/<target>` when present,
- merges `origin/<main>`,
- saves resume state with `mode: "switch"` on conflict,
- returns `ok` while staying on the target branch.

- [x] **Step 2: Make resume mode-aware**

Update `resume_one` so `mode == "switch"` commits resolved merge state and returns success while staying on target. Existing sync mode keeps push, checkout back, and stash pop behavior.

- [x] **Step 3: Update stash center tags**

Update `list_stashes` and `pop_stash` to recognize `sync-branches-switch: <orig>`.

- [x] **Step 4: Verify Python tests pass**

Run: `python3 -m unittest tests.test_create_branches -v`

Expected: all tests pass.

### Task 3: Web Page And API

**Files:**
- Modify: `sync-branches-ui.py`

- [x] **Step 1: Add SSE API**

Add `/api/switch_branch`, parse `projects`, `branch`, and `base`, emit entries, and run `sse_switch_branch`.

- [x] **Step 2: Add `/switch` page**

Extend page mode routing and navigation with `/switch`. Add project list, target branch input, switch button, and status span. Reuse result cards and copy summary behavior.

- [x] **Step 3: Smoke check page rendering**

Run:
- `python3 -m py_compile sync-branches-ui.py`
- Start server and curl `/switch` to verify `data-page="switch"` and `btnSwitch` exist.

### Task 4: Bash CLI

**Files:**
- Modify: `sync-branches.sh`
- Modify: `tests/test_create_branches_cli.sh`

- [x] **Step 1: Write failing CLI test**

Extend the shell integration test to run:

```bash
SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --switch dev_switch <<'EOF'
repo_switch
EOF
```

Assert that the repo ends on `dev_switch`, main branch changes are present, dirty work is in `sync-branches-switch`, and target branch missing returns non-zero.

- [x] **Step 2: Verify CLI test fails**

Run: `bash tests/test_create_branches_cli.sh`

Expected: failure because `--switch` does not exist.

- [x] **Step 3: Implement `--switch` mode**

Parse `--switch <branch>` before old sync parsing. Add `process_switch_project` mirroring the Python flow. Keep bash 3.2 syntax and use `${branch}` when Chinese punctuation follows variables.

- [x] **Step 4: Verify CLI test passes**

Run: `bash tests/test_create_branches_cli.sh`

Expected: shell integration test passes.

### Task 5: Docs, Bundles, Verification

**Files:**
- Modify: `README.md`
- Modify: `dist/README.md`
- Modify generated bundle files under `dist/`

- [x] **Step 1: Update docs**

Document `/switch`, `--switch <branch>`, stash behavior, and conflict behavior.

- [x] **Step 2: Build desktop bundles**

Run: `./scripts/build.sh`

Expected: Mac launcher, Windows Python copy, app signature, and zip refresh.

- [x] **Step 3: Final verification**

Run:
- `python3 -m py_compile sync-branches-ui.py dist/windows/sync-branches-ui.py dist/分支同步面板.app/Contents/Resources/sync-branches-ui.py`
- `python3 -m unittest tests.test_create_branches -v`
- `bash tests/test_create_branches_cli.sh`
- `bash -n sync-branches.sh`
- `git diff --check`

Expected: all commands exit 0.
