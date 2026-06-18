const test = require('node:test');
const assert = require('node:assert/strict');

const buildServer = require('../scripts/build-server');

test('pythonCandidates prefers setup-python command on Windows', () => {
  assert.deepEqual(buildServer.pythonCandidates('win32', {}), [
    { command: 'python', args: [] },
    { command: 'py', args: ['-3'] },
  ]);
});

test('pythonCandidates honors PYTHON override first', () => {
  assert.deepEqual(buildServer.pythonCandidates('darwin', { PYTHON: '/tmp/python' }), [
    { command: '/tmp/python', args: [] },
    { command: 'python3', args: [] },
    { command: 'python', args: [] },
  ]);
});
