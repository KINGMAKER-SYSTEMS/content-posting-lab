#!/usr/bin/env node
// Seekable HTML → MP4 renderer (production home of the deucebailey0 prototype).
// Contract: the composition exposes window.duration (seconds) and window.seek(t);
// every visual state must be derivable from t — no wall-clock, no setInterval.
//
//   node tools/seekable-html-video/render_seekable.cjs \
//     --input runs/my-video/index.html --output runs/my-video/final-16x9.mp4 \
//     [--frames-dir .frames] [--fps 24] [--width 1920] [--height 1080]
//     [--contact-sheet review/contact_start_mid_end.jpg]  start/mid/end QA strip
//     [--poster-time 7.5 --poster review/poster.png]      single-frame export
//     [--cleanup-frames]                                  delete frame dump on success
//
// Needs playwright on NODE_PATH (NODE_PATH=frontend/node_modules from repo root) and ffmpeg.
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const { pathToFileURL } = require("node:url");
const { chromium } = require("playwright");

function arg(name, fallback = null) {
  const idx = process.argv.indexOf(`--${name}`);
  if (idx === -1) return fallback;
  return process.argv[idx + 1] ?? fallback;
}
const flag = (name) => process.argv.includes(`--${name}`);

async function main() {
  const input = arg("input");
  const output = arg("output");
  // 16:9 YouTube defaults (the 9:16 prototype required these explicitly)
  const fps = Number(arg("fps", "24"));
  const width = Number(arg("width", "1920"));
  const height = Number(arg("height", "1080"));
  const framesDir = arg("frames-dir") || path.join(os.tmpdir(), `seekframes-${process.pid}`);
  const durationOverride = arg("duration");
  const contactSheet = arg("contact-sheet");
  const posterTime = arg("poster-time");
  const posterOut = arg("poster");

  if (!input || !output) {
    console.error("usage: node render_seekable.cjs --input index.html --output out.mp4 [--fps 24 --width 1920 --height 1080]");
    process.exit(2);
  }

  fs.rmSync(framesDir, { recursive: true, force: true });
  fs.mkdirSync(framesDir, { recursive: true });
  fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true });

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  await page.goto(pathToFileURL(path.resolve(input)).href, { waitUntil: "networkidle" });
  // fonts must be painted before frame 0 or early frames render in fallback glyphs
  await page.evaluate(() => (document.fonts ? document.fonts.ready : Promise.resolve()));
  const duration = durationOverride
    ? Number(durationOverride)
    : await page.evaluate(() => Number(window.duration || document.querySelector("[data-duration]")?.dataset.duration || 10));
  const totalFrames = Math.max(1, Math.round(duration * fps));

  for (let i = 0; i < totalFrames; i++) {
    const t = i / fps;
    await page.evaluate((time) => {
      if (typeof window.seek === "function") window.seek(time);
      else document.documentElement.style.setProperty("--time", String(time));
    }, t);
    const out = path.join(framesDir, `frame_${String(i).padStart(5, "0")}.png`);
    await page.screenshot({ path: out, type: "png" });
    if (i % 48 === 0) console.log(`frame ${i}/${totalFrames}`);
  }

  // poster: re-seek to the exact requested time so it isn't quantized to a frame index
  if (posterTime != null && posterOut) {
    await page.evaluate((time) => window.seek && window.seek(Number(time)), posterTime);
    fs.mkdirSync(path.dirname(path.resolve(posterOut)), { recursive: true });
    await page.screenshot({ path: posterOut, type: "png" });
    console.log(`poster @${posterTime}s → ${posterOut}`);
  }
  await browser.close();

  const ff = spawnSync("ffmpeg", [
    "-y",
    "-framerate", String(fps),
    "-i", path.join(framesDir, "frame_%05d.png"),
    "-an",
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-profile:v", "high",
    "-crf", "18",
    "-movflags", "+faststart",
    output,
  ], { stdio: "inherit" });
  if (ff.status !== 0) process.exit(ff.status || 1);

  if (contactSheet) {
    // start / mid / end QA strip — review these BEFORE polishing anything
    const pick = [0, Math.floor(totalFrames / 2), totalFrames - 1]
      .map((i) => path.join(framesDir, `frame_${String(i).padStart(5, "0")}.png`));
    fs.mkdirSync(path.dirname(path.resolve(contactSheet)), { recursive: true });
    const sheet = spawnSync("ffmpeg", [
      "-y",
      "-i", pick[0], "-i", pick[1], "-i", pick[2],
      "-filter_complex", "[0][1][2]hstack=inputs=3,scale=1920:-1",
      "-frames:v", "1", contactSheet,
    ], { stdio: "inherit" });
    if (sheet.status === 0) console.log(`contact sheet → ${contactSheet}`);
  }

  if (flag("cleanup-frames")) fs.rmSync(framesDir, { recursive: true, force: true });
  console.log(output);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
