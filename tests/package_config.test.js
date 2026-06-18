const test = require('node:test');
const assert = require('node:assert/strict');

const packageJson = require('../package.json');

test('electron-builder artifacts use stable release file names', () => {
  assert.equal(packageJson.build.artifactName, 'sync-branch-${version}-${os}-${arch}.${ext}');
});
