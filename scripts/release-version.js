#!/usr/bin/env node

function expectedTag(version) {
  return `v${version}`;
}

function assertTagMatchesVersion(tag, version) {
  const expected = expectedTag(version);
  if (tag !== expected) {
    throw new Error(`Release tag ${tag} does not match package version ${version}. Expected ${expected}.`);
  }
}

function main() {
  const version = require('../package.json').version;
  const tag = process.argv[2];

  if (!tag) {
    process.stdout.write(version);
    return;
  }

  assertTagMatchesVersion(tag, version);
  process.stdout.write(version);
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}

module.exports = {
  assertTagMatchesVersion,
  expectedTag,
};
