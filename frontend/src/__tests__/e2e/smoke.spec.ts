import { execFileSync } from 'node:child_process';
import { mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect, test } from '@playwright/test';

const projectName = 'e2e-flow';
const repoRoot = path.resolve(process.cwd(), '..');

async function deleteProjectViaApi(name: string) {
  await fetch(`http://127.0.0.1:8000/api/projects/${encodeURIComponent(name)}`, { method: 'DELETE' }).catch(() => null);
}

async function seedArtifacts() {
  const projectRoot = path.join(repoRoot, 'projects', projectName);
  const videoDir = path.join(projectRoot, 'videos', 'seed');
  const captionDir = path.join(projectRoot, 'captions', 'artist1');

  await mkdir(videoDir, { recursive: true });
  await mkdir(captionDir, { recursive: true });

  const videoPath = path.join(videoDir, 'clip.mp4');
  execFileSync(
    'ffmpeg',
    ['-f', 'lavfi', '-i', 'color=c=black:s=1080x1920:d=1', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-y', videoPath],
    { stdio: 'ignore' },
  );

  await writeFile(
    path.join(captionDir, 'captions.csv'),
    'video_id,video_url,caption,error\n1,https://tiktok.com/@artist1/video/1,seed caption,\n',
    'utf-8',
  );
}

async function assertBurnedOutputExists() {
  const burnRoot = path.join(repoRoot, 'projects', projectName, 'burned');
  const batches = await readdir(burnRoot);
  expect(batches.length).toBeGreaterThan(0);
  const latestBatch = path.join(burnRoot, batches[0]);
  const outputs = await readdir(latestBatch);
  expect(outputs.some((file) => file.endsWith('.mp4'))).toBeTruthy();
}

test.beforeEach(async () => {
  await deleteProjectViaApi(projectName);
  await rm(path.join(repoRoot, 'projects', projectName), { recursive: true, force: true });
});

test.afterEach(async () => {
  await deleteProjectViaApi(projectName);
  await rm(path.join(repoRoot, 'projects', projectName), { recursive: true, force: true });
});

test('seeded workflow: create project -> burn -> verify output', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByText('Content Posting Lab')).toBeVisible();

  await page.locator('main').getByRole('button', { name: 'New Project' }).click();
  await page.getByLabel('Project Name').fill(projectName);
  await page.getByRole('button', { name: 'Create Project' }).click();
  await expect(page.getByRole('heading', { name: 'Create New Project' })).toBeHidden();
  await expect(page.getByRole('heading', { name: projectName }).first()).toBeVisible();

  await seedArtifacts();

  await page.getByRole('link', { name: 'Burn' }).click();
  await expect(page.getByRole('heading', { name: 'Burn Captions' })).toBeVisible();

  await page.waitForTimeout(500);
  await page.evaluate(() => window.dispatchEvent(new Event('burn:refresh-request')));

  await expect(page.locator('option', { hasText: 'seed/clip.mp4' })).toHaveCount(1);
  await expect(page.locator('option', { hasText: '@artist1 (1 captions)' })).toHaveCount(1);

  await page.getByRole('button', { name: 'Burn Caption' }).click();
  await expect(page.getByText('Caption burned successfully')).toBeVisible({ timeout: 20000 });

  await assertBurnedOutputExists();
});
