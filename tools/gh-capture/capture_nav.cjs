#!/usr/bin/env node
/* GH page-nav capture: scripted playwright session recorded to video.
   Usage: node capture_nav.cjs --url <github url> --out <out.mp4> [--seconds 30]
   Records: header hold (stars visible) -> smooth README scroll -> hold on a
   content anchor. Output re-encoded to 1920x1080 24fps yuv420p. */
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { validateGitHubUrl, isAllowedRequest, parseSeconds, resolveOutputPath } = require('./target_policy.cjs');

const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, '');
  if (!['url', 'out', 'seconds'].includes(key) || !process.argv[i + 1] || args[key]) {
    console.error('expected unique --url, --out, and optional --seconds arguments');
    process.exit(1);
  }
  args[key] = process.argv[i + 1];
}
let URL, OUT, SECONDS;
try {
  URL = validateGitHubUrl(args.url);
  OUT = resolveOutputPath(args.out, path.resolve(__dirname, '../..'));
  SECONDS = parseSeconds(args.seconds || '30');
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

(async () => {
  const tmpdir = fs.mkdtempSync('/tmp/ghnav-');
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const ctx = await browser.newContext({
      viewport: { width: 1920, height: 1080 },
      recordVideo: { dir: tmpdir, size: { width: 1920, height: 1080 } },
      colorScheme: 'dark',
      deviceScaleFactor: 1,
    });
    const page = await ctx.newPage();
    await page.route('**/*', async (route) => {
      if (isAllowedRequest(route.request().url())) await route.continue();
      else await route.abort('blockedbyclient');
    });
    await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 45000 });
    if (new URL(page.url()).hostname !== 'github.com') throw new Error('navigation left github.com');
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
    browser = null;
    const webm = fs.readdirSync(tmpdir).find(f => f.endsWith('.webm'));
    if (!webm) throw new Error('no video produced');
    execFileSync('ffmpeg', ['-n', '-loglevel', 'error', '-i', path.join(tmpdir, webm),
      '-vf', 'scale=1920:1080,fps=24,format=yuv420p',
      '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-an',
      '-video_track_timescale', '24000', OUT]);
    const probe = execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', OUT]).toString().trim();
    console.log(`OK ${OUT} ${probe}s`);
  } finally {
    if (browser) await browser.close().catch(() => {});
    fs.rmSync(tmpdir, { recursive: true, force: true });
  }
})().catch(e => { console.error(e.message); process.exit(1); });
