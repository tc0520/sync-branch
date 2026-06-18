const test = require('node:test');
const assert = require('node:assert/strict');

const releaseVersion = require('../scripts/release-version');

test('expectedTag prefixes package version with v', () => {
  assert.equal(releaseVersion.expectedTag('1.0.0'), 'v1.0.0');
});

test('assertTagMatchesVersion accepts matching release tag', () => {
  assert.doesNotThrow(() => releaseVersion.assertTagMatchesVersion('v1.0.0', '1.0.0'));
});

test('assertTagMatchesVersion rejects stale release tag', () => {
  assert.throws(
    () => releaseVersion.assertTagMatchesVersion('v1.0.1', '1.0.0'),
    /Release tag v1\.0\.1 does not match package version 1\.0\.0/,
  );
});
