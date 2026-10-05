"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { prepareFramesDir } = require("./frame_directory.cjs");

test("--frames-dir . refuses existing contents without deleting them", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-cwd-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const sentinel = path.join(cwd, "keep-me.txt");
  fs.writeFileSync(sentinel, "valuable working-directory data");

  assert.throws(() => prepareFramesDir(".", { cwd }), /Refusing non-empty/);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "valuable working-directory data");
  assert.deepEqual(fs.readdirSync(cwd), ["keep-me.txt"]);
});

test("an arbitrary caller directory's contents survive a rejected render", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-arbitrary-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const framesDir = path.join(cwd, "user-assets");
  fs.mkdirSync(framesDir);
  const sentinel = path.join(framesDir, "source.json");
  fs.writeFileSync(sentinel, "keep this source");

  assert.throws(() => prepareFramesDir(framesDir, { cwd }), /Refusing non-empty/);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "keep this source");
  assert.deepEqual(fs.readdirSync(framesDir), ["source.json"]);
});

test("cleanup removes only frames written by this render from a caller-owned directory", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-cleanup-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const framesDir = path.join(cwd, "frames");
  fs.mkdirSync(framesDir);
  const workspace = prepareFramesDir(framesDir, { cwd });
  const frame = path.join(framesDir, "frame_00000.png");
  workspace.writeFrame(frame, "rendered frame");
  const sentinel = path.join(framesDir, "keep.txt");
  fs.writeFileSync(sentinel, "caller data");

  workspace.cleanup();
  assert.equal(fs.existsSync(frame), false);
  assert.equal(fs.readFileSync(sentinel, "utf8"), "caller data");
  assert.equal(fs.existsSync(framesDir), true);
});


test("exclusive frame writes preserve a file created after directory validation", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-race-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const framesDir = path.join(cwd, "frames");
  fs.mkdirSync(framesDir);
  const workspace = prepareFramesDir(framesDir, { cwd });
  const frame = path.join(framesDir, "frame_00000.png");
  fs.writeFileSync(frame, "concurrent caller data");

  assert.throws(() => workspace.writeFrame(frame, "rendered frame"), (error) => error.code === "EEXIST");
  workspace.cleanup();
  assert.equal(fs.readFileSync(frame, "utf8"), "concurrent caller data");
});

test("cleanup leaves a caller replacement at a generated frame path", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-replaced-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const framesDir = path.join(cwd, "frames");
  fs.mkdirSync(framesDir);
  const workspace = prepareFramesDir(framesDir, { cwd });
  const frame = path.join(framesDir, "frame_00000.png");
  const replacement = path.join(framesDir, "replacement");
  workspace.writeFrame(frame, "rendered frame");
  fs.writeFileSync(replacement, "new caller file");
  fs.unlinkSync(frame);
  fs.renameSync(replacement, frame);

  workspace.cleanup();
  assert.equal(fs.readFileSync(frame, "utf8"), "new caller file");
});


test("a caller directory replaced after validation is neither written through nor cleaned", (t) => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-dir-replaced-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const framesDir = path.join(cwd, "frames");
  const replacement = path.join(cwd, "replacement");
  fs.mkdirSync(framesDir);
  fs.mkdirSync(replacement);
  const sentinel = path.join(replacement, "keep.txt");
  fs.writeFileSync(sentinel, "replacement data");
  const workspace = prepareFramesDir(framesDir, { cwd });
  const moved = path.join(cwd, "validated-frames");
  fs.renameSync(framesDir, moved);
  fs.symlinkSync(replacement, framesDir);
  const frame = path.join(framesDir, "frame_00000.png");

  assert.throws(() => workspace.writeFrame(frame, "rendered frame"), /changed during rendering/);
  workspace.cleanup();
  assert.equal(fs.readFileSync(sentinel, "utf8"), "replacement data");
  assert.deepEqual(fs.readdirSync(replacement), ["keep.txt"]);
  assert.deepEqual(fs.readdirSync(moved), []);
});

test("managed temporary frame directory is cleaned with its generated contents", (t) => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "seekframes-managed-"));
  t.after(() => fs.rmSync(tempDir, { recursive: true, force: true }));
  const workspace = prepareFramesDir(null, { tempDir });
  const frame = path.join(workspace.framesDir, "frame_00000.png");
  workspace.writeFrame(frame, "rendered frame");

  workspace.cleanup();
  assert.equal(fs.existsSync(workspace.framesDir), false);
});
