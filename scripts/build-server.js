#!/usr/bin/env node
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const serverDir = path.join(root, 'electron', 'resources', 'server');
const buildDir = path.join(root, 'build', 'pyinstaller');
const scriptPath = path.join(root, 'sync-branches-ui.py');

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: 'utf8',
    stdio: options.stdio || 'pipe',
    shell: false,
  });
  return result;
}

function pythonCandidates(platform = process.platform, env = process.env) {
  const candidates = [];
  if (env.PYTHON) {
    candidates.push({ command: env.PYTHON, args: [] });
  }

  if (platform === 'win32') {
    candidates.push(
      { command: 'python', args: [] },
      { command: 'py', args: ['-3'] },
    );
  } else {
    candidates.push(
      { command: 'python3', args: [] },
      { command: 'python', args: [] },
    );
  }

  return candidates;
}

function findPython(platform = process.platform, env = process.env) {
  const candidates = pythonCandidates(platform, env);

  for (const candidate of candidates) {
    const result = run(candidate.command, [...candidate.args, '--version']);
    if (result.status === 0) {
      return candidate;
    }
  }
  return null;
}

function assertPyInstaller(python) {
  const result = run(python.command, [
    ...python.args,
    '-m',
    'PyInstaller',
    '--version',
  ]);
  if (result.status !== 0) {
    const pythonCmd = [python.command, ...python.args].join(' ');
    console.error('未找到 PyInstaller。请先执行：');
    console.error(`${pythonCmd} -m pip install pyinstaller`);
    process.exit(1);
  }
}

function main() {
  const python = findPython();
  if (!python) {
    console.error('未找到 Python。请先安装 Python 3。');
    process.exit(1);
  }

  assertPyInstaller(python);
  fs.rmSync(serverDir, { recursive: true, force: true });
  fs.mkdirSync(serverDir, { recursive: true });
  fs.mkdirSync(buildDir, { recursive: true });

  const args = [
    ...python.args,
    '-m',
    'PyInstaller',
    '--onefile',
    '--clean',
    '--name',
    'sync-branches-server',
    '--distpath',
    serverDir,
    '--workpath',
    path.join(buildDir, 'work'),
    '--specpath',
    buildDir,
    scriptPath,
  ];

  console.log('构建内置 Python 后端...');
  const result = spawnSync(python.command, args, {
    cwd: root,
    stdio: 'inherit',
    shell: false,
  });
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
  console.log(`已生成: ${serverDir}`);
}

if (require.main === module) {
  main();
}

module.exports = {
  findPython,
  pythonCandidates,
};
