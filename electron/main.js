const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  buildServerEnv,
  devServerCommand,
  packagedServerCommand,
  resourceServerPath,
  serviceUrl,
} = require('./launcher');

const PORT = Number(process.env.SYNC_BRANCHES_PORT || 8799);
const START_TIMEOUT_MS = 12000;
const POLL_INTERVAL_MS = 250;

let mainWindow = null;
let serverProcess = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function isServiceReady(url) {
  try {
    const res = await fetch(url, { method: 'GET' });
    return res.ok;
  } catch (_err) {
    return false;
  }
}

async function waitForService(url, timeoutMs = START_TIMEOUT_MS) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await isServiceReady(url)) {
      return true;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  return false;
}

function serverCommand() {
  if (app.isPackaged) {
    return packagedServerCommand(process.resourcesPath, process.platform, PORT);
  }

  const repoRoot = path.resolve(__dirname, '..');
  const bundledDevServer = resourceServerPath(path.join(repoRoot, 'electron', 'resources'), process.platform);
  if (fs.existsSync(bundledDevServer)) {
    return {
      command: bundledDevServer,
      args: [String(PORT)],
    };
  }

  return devServerCommand(repoRoot, PORT);
}

function startServer() {
  const { command, args } = serverCommand();
  serverProcess = spawn(command, args, {
    cwd: app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..'),
    env: buildServerEnv(process.env, os.homedir()),
    stdio: app.isPackaged ? 'ignore' : 'inherit',
    windowsHide: true,
  });

  serverProcess.on('exit', () => {
    serverProcess = null;
  });
}

function stopServer() {
  if (serverProcess && !serverProcess.killed) {
    serverProcess.kill();
  }
  serverProcess = null;
}

function focusMainWindow() {
  if (!mainWindow) {
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

async function createWindow() {
  const url = serviceUrl(PORT);
  if (!(await isServiceReady(url))) {
    startServer();
  }

  const ready = await waitForService(url);
  if (!ready) {
    dialog.showErrorBox(
      '分支同步面板启动失败',
      '本地后端服务没有启动成功。请确认 Git 已安装，或从命令行运行应用查看详细错误。',
    );
    app.quit();
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1120,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    title: '分支同步面板',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  await mainWindow.loadURL(url);
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', focusMainWindow);
  app.whenReady().then(createWindow);

  app.on('window-all-closed', () => {
    app.quit();
  });

  app.on('before-quit', stopServer);
}
