"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

function samePathEntry(filePath, expected) {
  try {
    const actual = fs.lstatSync(filePath);
    return actual.dev === expected.dev && actual.ino === expected.ino;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

function sameDirectoryEntry(directory, expected) {
  if (!samePathEntry(directory, expected)) return false;
  const actual = fs.lstatSync(directory);
  return actual.isDirectory() && !actual.isSymbolicLink();
}

function assertDirectoryEntry(directory, expected) {
  if (!sameDirectoryEntry(directory, expected)) {
    throw new Error("The selected --frames-dir changed during rendering: " + directory);
  }
}

function prepareFramesDir(requestedPath, options = {}) {
  const tempDir = options.tempDir || os.tmpdir();
  const cwd = options.cwd || process.cwd();
  const files = new Map();

  if (requestedPath == null || requestedPath === "") {
    const framesDir = fs.mkdtempSync(path.join(tempDir, "seekframes-" + process.pid + "-"));
    const identity = fs.lstatSync(framesDir);
    return {
      framesDir,
      writeFrame(filePath, contents) {
        assertFramePath(filePath, framesDir);
        writeFrame(filePath, contents, files);
      },
      cleanup() {
        if (samePathEntry(framesDir, identity)) {
          fs.rmSync(framesDir, { recursive: true, force: true });
        }
      },
    };
  }

  const framesDir = path.resolve(cwd, requestedPath);
  let existing;
  try {
    existing = fs.lstatSync(framesDir);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  if (existing) {
    if (!existing.isDirectory() || existing.isSymbolicLink()) {
      throw new Error("Refusing --frames-dir because it is not a real directory: " + framesDir);
    }
    if (fs.readdirSync(framesDir).length !== 0) {
      throw new Error("Refusing non-empty --frames-dir to protect existing files: " + framesDir);
    }
  } else {
    fs.mkdirSync(framesDir, { recursive: true });
  }

  const directoryIdentity = fs.lstatSync(framesDir);
  if (!directoryIdentity.isDirectory() || directoryIdentity.isSymbolicLink()) {
    throw new Error("Refusing --frames-dir because it is not a real directory: " + framesDir);
  }

  return {
    framesDir,
    writeFrame(filePath, contents) {
      assertFramePath(filePath, framesDir);
      assertDirectoryEntry(framesDir, directoryIdentity);
      writeFrame(filePath, contents, files);
      assertDirectoryEntry(framesDir, directoryIdentity);
    },
    cleanup() {
      if (!sameDirectoryEntry(framesDir, directoryIdentity)) return;
      for (const [filePath, identity] of files) {
        if (samePathEntry(filePath, identity)) fs.unlinkSync(filePath);
      }
    },
  };
}

function assertFramePath(filePath, framesDir) {
  const resolved = path.resolve(filePath);
  if (path.dirname(resolved) !== framesDir || !/^frame_\d{5}\.png$/.test(path.basename(resolved))) {
    throw new Error("Refusing frame path outside the managed frame naming contract: " + filePath);
  }
}

function writeFrame(filePath, contents, files) {
  fs.writeFileSync(filePath, contents, { flag: "wx", mode: 0o600 });
  files.set(filePath, fs.lstatSync(filePath));
}

module.exports = { prepareFramesDir };
