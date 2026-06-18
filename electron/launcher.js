const path = require('node:path');

function serverExecutableName(platform = process.platform) {
  return platform === 'win32' ? 'sync-branches-server.exe' : 'sync-branches-server';
}

function resourceServerPath(resourcesPath, platform = process.platform) {
  return path.join(resourcesPath, 'server', serverExecutableName(platform));
}

function packagedServerCommand(resourcesPath, platform, port) {
  return {
    command: resourceServerPath(resourcesPath, platform),
    args: [String(port)],
  };
}

function devServerCommand(repoRoot, port, pythonCommand = process.env.PYTHON || 'python3') {
  return {
    command: pythonCommand,
    args: [path.join(repoRoot, 'sync-branches-ui.py'), String(port)],
  };
}

function buildServerEnv(baseEnv = process.env, homeDir) {
  return {
    ...baseEnv,
    SYNC_NO_BROWSER: '1',
    SYNC_DEFAULT_BASE: homeDir,
  };
}

function serviceUrl(port) {
  return `http://127.0.0.1:${port}/sync`;
}

module.exports = {
  buildServerEnv,
  devServerCommand,
  packagedServerCommand,
  resourceServerPath,
  serverExecutableName,
  serviceUrl,
};
