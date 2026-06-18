const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const packageJson = require('../package.json');

test('electron-builder artifacts use stable release file names', () => {
  assert.equal(packageJson.build.artifactName, 'sync-branch-${version}-${os}-${arch}.${ext}');
});

test('release workflow deletes old assets through API urls', () => {
  const workflowPath = path.join(__dirname, '..', '.github', 'workflows', 'build.yml');
  const workflow = fs.readFileSync(workflowPath, 'utf8');

  assert.match(workflow, /gh api --method DELETE/);
});
