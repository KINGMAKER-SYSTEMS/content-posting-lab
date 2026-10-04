'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { validateGitHubUrl, isAllowedRequest, parseSeconds, resolveOutputPath } = require('./target_policy.cjs');

test('capture accepts canonical GitHub repository and content URLs', () => {
  assert.equal(validateGitHubUrl('https://github.com/owner/repo'), 'https://github.com/owner/repo');
  assert.equal(validateGitHubUrl('https://github.com/owner/repo/tree/main/examples'),
    'https://github.com/owner/repo/tree/main/examples');
});

test('capture rejects arbitrary, deceptive, and command-injection URLs', () => {
  for (const value of [
    'http://github.com/owner/repo',
    'https://github.com.evil.test/owner/repo',
    'https://github.com@127.0.0.1/owner/repo',
    'https://github.com:444/owner/repo',
    'https://github.com/owner/repo?next=https://attacker.test',
    'https://github.com/owner/repo#fragment',
    'javascript:alert(1)',
    'file:///etc/passwd',
    'https://github.com/owner/repo;$(touch${IFS}/tmp/pwned)',
    'https://github.com/owner/%2e%2e/private',
  ]) assert.throws(() => validateGitHubUrl(value));
});

test('browser requests stay on HTTPS GitHub and its fixed asset hosts', () => {
  assert.equal(isAllowedRequest('https://github.com/owner/repo'), true);
  assert.equal(isAllowedRequest('https://avatars.githubusercontent.com/u/1'), true);
  for (const value of [
    'http://github.com/owner/repo',
    'https://evil.test/',
    'https://github.com.evil.test/',
    'https://127.0.0.1/',
    'https://github.com:444/',
    'file:///etc/passwd',
  ]) assert.equal(isAllowedRequest(value), false, value);
});

test('duration is bounded to integer capture seconds', () => {
  assert.equal(parseSeconds('32'), 32);
  for (const value of ['9', '46', '30.5', 'NaN', '-1']) assert.throws(() => parseSeconds(value));
});

test('output is constrained to the repository and cannot overwrite existing files', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gh-capture-policy-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const outputDir = path.join(root, 'footage');
  fs.mkdirSync(outputDir);
  assert.equal(resolveOutputPath(path.join(outputDir, 'capture.mp4'), root),
    path.join(fs.realpathSync(outputDir), 'capture.mp4'));
  for (const value of [
    path.join(os.tmpdir(), 'outside.mp4'),
    path.join(outputDir, 'capture.mov'),
  ]) assert.throws(() => resolveOutputPath(value, root));
  fs.writeFileSync(path.join(outputDir, 'existing.mp4'), 'keep');
  assert.throws(() => resolveOutputPath(path.join(outputDir, 'existing.mp4'), root), /already exists/);
});

test('output directory symlink cannot escape the repository', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gh-capture-policy-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'gh-capture-outside-'));
  t.after(() => {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  });
  fs.symlinkSync(outside, path.join(root, 'escape'));
  assert.throws(() => resolveOutputPath(path.join(root, 'escape', 'capture.mp4'), root), /inside the repository/);
});
