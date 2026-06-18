const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const launcher = require('../electron/launcher');

test('serverExecutableName uses .exe only on Windows', () => {
  assert.equal(launcher.serverExecutableName('win32'), 'sync-branches-server.exe');
  assert.equal(launcher.serverExecutableName('darwin'), 'sync-branches-server');
  assert.equal(launcher.serverExecutableName('linux'), 'sync-branches-server');
});

test('resourceServerPath points into Electron resources', () => {
  const result = launcher.resourceServerPath('/tmp/resources', 'win32');

  assert.equal(
    result,
    path.join('/tmp/resources', 'server', 'sync-branches-server.exe'),
  );
});

test('packagedServerCommand runs bundled backend executable with port', () => {
  const result = launcher.packagedServerCommand('/tmp/resources', 'darwin', 8799);

  assert.deepEqual(result, {
    command: path.join('/tmp/resources', 'server', 'sync-branches-server'),
    args: ['8799'],
  });
});

test('devServerCommand runs the source script through Python', () => {
  const result = launcher.devServerCommand('/repo/root', 9001, 'python3');

  assert.deepEqual(result, {
    command: 'python3',
    args: [path.join('/repo/root', 'sync-branches-ui.py'), '9001'],
  });
});

test('buildServerEnv disables browser auto-open and sets default base', () => {
  const env = launcher.buildServerEnv({ PATH: '/bin' }, '/Users/demo');

  assert.equal(env.PATH, '/bin');
  assert.equal(env.SYNC_NO_BROWSER, '1');
  assert.equal(env.SYNC_DEFAULT_BASE, '/Users/demo');
});

test('serviceUrl opens the sync page on localhost', () => {
  assert.equal(launcher.serviceUrl(8799), 'http://127.0.0.1:8799/sync');
});
