# Electron Bundled Python Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Windows `.bat` and macOS Swift shell distribution path with an Electron desktop app that bundles the existing Python Web engine as a native backend executable.

**Architecture:** Keep `sync-branches-ui.py` as the business-logic source of truth. Build it with PyInstaller into a platform-native `sync-branches-server` executable, package that executable as an Electron resource, and have Electron manage single-instance locking, backend startup, window lifecycle, and backend shutdown.

**Tech Stack:** Electron, electron-builder, Node.js `child_process`, PyInstaller, Python 3, Git CLI.

---

### Task 1: Electron Launcher Tests

**Files:**
- Create: `tests/electron_launcher.test.js`
- Create: `electron/launcher.js`

- [ ] **Step 1: Write failing tests**

Create tests for the pure helper layer:

```js
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const launcher = require('../electron/launcher');

test('serverExecutableName uses .exe only on Windows', () => {
  assert.equal(launcher.serverExecutableName('win32'), 'sync-branches-server.exe');
  assert.equal(launcher.serverExecutableName('darwin'), 'sync-branches-server');
});

test('buildServerEnv disables browser auto-open and sets default base', () => {
  const env = launcher.buildServerEnv({ PATH: '/bin' }, '/Users/demo');
  assert.equal(env.PATH, '/bin');
  assert.equal(env.SYNC_NO_BROWSER, '1');
  assert.equal(env.SYNC_DEFAULT_BASE, '/Users/demo');
});
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `node --test tests/electron_launcher.test.js`

Expected: FAIL because `electron/launcher.js` does not exist.

### Task 2: Electron Launcher Implementation

**Files:**
- Create: `electron/launcher.js`
- Create: `electron/main.js`
- Create: `package.json`

- [ ] **Step 1: Implement launcher helpers**

Implement pure helpers for:
- `serverExecutableName(platform)`
- `resourceServerPath(resourcesPath, platform)`
- `devServerCommand(repoRoot, port)`
- `packagedServerCommand(resourcesPath, platform, port)`
- `buildServerEnv(baseEnv, homeDir)`
- `serviceUrl(port)`

- [ ] **Step 2: Implement Electron main process**

`electron/main.js` should:
- request a single instance lock,
- reuse/focus the first window on repeated launches,
- check whether `http://127.0.0.1:8799/sync` is already reachable,
- spawn the bundled backend executable when needed,
- pass `SYNC_NO_BROWSER=1` and `SYNC_DEFAULT_BASE=<home>`,
- use `windowsHide: true`,
- load the panel URL in a `BrowserWindow`,
- kill the backend child process on app exit.

- [ ] **Step 3: Add npm scripts and electron-builder config**

`package.json` should include:
- `main: "electron/main.js"`
- `start: "electron ."`
- `test:electron: "node --test tests/electron_launcher.test.js"`
- `build:server: "node scripts/build-server.js"`
- `dist:electron: "npm run build:server && electron-builder"`
- electron-builder `extraResources` entry for `electron/resources/server`.

- [ ] **Step 4: Run launcher tests**

Run: `npm run test:electron`

Expected: PASS.

### Task 3: PyInstaller Backend Build Script

**Files:**
- Create: `scripts/build-server.js`
- Modify: `.gitignore`

- [ ] **Step 1: Implement backend build script**

`scripts/build-server.js` should:
- find `python3`, `python`, or `py -3`,
- verify PyInstaller is importable,
- clean `electron/resources/server`,
- run PyInstaller with `--onefile --clean --name sync-branches-server`,
- write the binary into `electron/resources/server`,
- print a clear install hint if PyInstaller is missing.

- [ ] **Step 2: Ignore generated artifacts**

Ignore:
- `node_modules/`
- `package-lock.json`
- `build/`
- `electron/resources/server/`
- `dist/electron/`

- [ ] **Step 3: Verify script syntax**

Run: `node --check scripts/build-server.js`

Expected: PASS.

### Task 4: Documentation And Distribution Notes

**Files:**
- Modify: `README.md`
- Modify: `dist/README.md`

- [ ] **Step 1: Document the new distribution path**

Explain:
- Electron app bundles the Python backend executable, so users do not install Python.
- Git is still required.
- Windows/macOS packages should be built on the target OS or CI matrix because PyInstaller produces platform-native binaries.
- Old `.bat` and Swift paths remain legacy until the Electron package replaces them.

- [ ] **Step 2: Document build commands**

Add:

```bash
npm install
python3 -m pip install pyinstaller
npm run dist:electron
```

### Task 5: Verification

**Files:**
- All touched files.

- [ ] **Step 1: Run JS tests**

Run: `npm run test:electron`

Expected: PASS.

- [ ] **Step 2: Run Python and shell regression tests**

Run:

```bash
python3 -m unittest tests.test_create_branches tests.test_stash_detail -v
bash tests/test_create_branches_cli.sh
bash -n sync-branches.sh tests/test_create_branches_cli.sh scripts/build.sh
python3 -m py_compile sync-branches-ui.py
git diff --check
```

Expected: all commands succeed.
