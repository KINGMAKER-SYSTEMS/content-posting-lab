#!/usr/bin/env node
/* GH page-nav capture: scripted playwright session recorded to video.
   Usage: node capture_nav.cjs --url <github url> --out <out.mp4> [--seconds 30]
   Records: header hold (stars visible) -> smooth README scroll -> hold on a
   content anchor. Output re-encoded to 1920x1080 24fps yuv420p. */
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const args = {};
for (let i = 2; i < process.argv.length; i += 2) args[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
const URL = args.url, OUT = args.out, SECONDS = parseFloat(args.seconds || '30');
if (!URL || !OUT) { console.error('need --url and --out'); process.exit(1); }

(async () => {
  const tmpdir = fs.mkdtempSync('/tmp/ghnav-');
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    recordVideo: { dir: tmpdir, size: { width: 1920, height: 1080 } },
    colorScheme: 'dark',
    deviceScaleFactor: 1,
  });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForTimeout(2500); // settle, lazy content

  // dismiss cookie banner if present (best effort)
  try { await page.locator('button:has-text("Accept")').first().click({ timeout: 1500 }); } catch (e) {}

  // header hold (stars/about visible)
  await page.waitForTimeout(3000);

  // smooth scroll through README: small steps, real-time capture
  const totalMs = Math.max(8000, (SECONDS - 8) * 1000);
  const steps = Math.floor(totalMs / 120);
  const pageHeight = await page.evaluate(() => document.body.scrollHeight);
  const target = Math.min(pageHeight - 1080, 9000);
  for (let i = 0; i < steps; i++) {
    // ease-in-out pacing: slower at start/end
    const p = i / steps;
    const ease = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
    await page.evaluate((y) => window.scrollTo(0, y), Math.round(target * ease));
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(2500); // end hold
  await ctx.close();
  await browser.close();

  const webm = fs.readdirSync(tmpdir).find(f => f.endsWith('.webm'));
  if (!webm) { console.error('no video produced'); process.exit(1); }
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', path.join(tmpdir, webm),
    '-vf', 'scale=1920:1080,fps=24,format=yuv420p',
    '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-an',
    '-video_track_timescale', '24000', OUT]);
  fs.rmSync(tmpdir, { recursive: true, force: true });
  const probe = execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', OUT]).toString().trim();
  console.log(`OK ${OUT} ${probe}s`);
})().catch(e => { console.error(e.message); process.exit(1); });
