'use strict';

const fs = require('fs');
const path = require('path');

const CAPTURE_HOSTS = new Set([
  'github.com',
  'github.githubassets.com',
  'avatars.githubusercontent.com',
  'raw.githubusercontent.com',
  'objects.githubusercontent.com',
  'user-images.githubusercontent.com',
  'camo.githubusercontent.com',
  'private-user-images.githubusercontent.com',
]);

function validateGitHubUrl(value) {
  if (typeof value !== 'string' || value.length > 2048) {
    throw new Error('capture URL must be a string no longer than 2048 characters');
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('capture URL must be a valid GitHub repository URL');
  }

  if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com' ||
      parsed.port || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('capture URL must use https://github.com without credentials, port, query, or fragment');
  }

  const segments = parsed.pathname.split('/').filter(Boolean);
  if (segments.length < 2 || segments.length > 12 || segments.some((segment) => {
    let decoded;
    try { decoded = decodeURIComponent(segment); } catch { return true; }
    return decoded === '.' || decoded === '..' || decoded.includes('/') ||
      !/^[A-Za-z0-9_.-]{1,100}$/.test(decoded);
  })) {
    throw new Error('capture URL must identify a GitHub repository path');
  }
  return parsed.href;
}

function isAllowedRequest(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && CAPTURE_HOSTS.has(parsed.hostname) && !parsed.port &&
      !parsed.username && !parsed.password;
  } catch {
    return false;
  }
}

function parseSeconds(value) {
  const seconds = Number(value);
  if (!Number.isInteger(seconds) || seconds < 10 || seconds > 45) {
    throw new Error('capture duration must be an integer from 10 to 45 seconds');
  }
  return seconds;
}

function resolveOutputPath(value, repoRoot) {
  if (typeof value !== 'string' || !value || value.length > 4096) {
    throw new Error('output path must be a non-empty path');
  }
  const repoPath = path.resolve(repoRoot);
  const root = fs.realpathSync(repoPath);
  const requested = path.resolve(value);
  const requestedParent = path.dirname(requested);
  const isWithin = (base, target) => {
    const relative = path.relative(base, target);
    return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
      ? relative
      : null;
  };
  const relative = isWithin(repoPath, requestedParent) ?? isWithin(root, requestedParent);
  if (relative === null) {
    throw new Error('output directory must be inside the repository');
  }
  const candidate = path.join(root, relative, path.basename(requested));
  if (path.extname(candidate).toLowerCase() !== '.mp4') {
    throw new Error('output file must use the .mp4 extension');
  }

  let parent = root;
  for (const component of relative.split(path.sep).filter(Boolean)) {
    parent = path.join(parent, component);
    try {
      fs.mkdirSync(parent, { mode: 0o755 });
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    const stat = fs.lstatSync(parent);
    if (!stat.isDirectory() || stat.isSymbolicLink() || fs.realpathSync(parent) !== parent) {
      throw new Error('output directory must use real directories inside the repository');
    }
  }

  try {
    fs.lstatSync(candidate);
    throw new Error('output file already exists');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  return path.join(parent, path.basename(candidate));
}

module.exports = { validateGitHubUrl, isAllowedRequest, parseSeconds, resolveOutputPath };
