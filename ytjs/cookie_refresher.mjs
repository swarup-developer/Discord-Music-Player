// Server-side YouTube cookie refresher for dcBot.
//
// Runs a headless Chromium with a persistent profile (ytjs/.browser-profile),
// opens YouTube and YouTube Music every REFRESH_HOURS through YOUTUBE_PROXY
// (Cloudflare WARP), and atomically replaces ../cookies.txt with the fresh
// cookies -- the old file is overwritten in one rename, never half-written.
//
// Without a Google login the profile holds anonymous visitor cookies, which is
// all playback needs behind WARP. If that profile is ever signed in, the same
// loop keeps the login cookies rotated and fresh automatically.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const COOKIE_FILE = path.join(ROOT, 'cookies.txt');
const PROFILE_DIR = path.join(HERE, '.browser-profile');
const REFRESH_HOURS = Number(process.env.COOKIE_REFRESH_HOURS || 6);
const MARKER = '# dcbot-cookie-refresher';

const log = (...a) => console.log(new Date().toISOString(), ...a);

function readProxy() {
  if (process.env.YOUTUBE_PROXY) return process.env.YOUTUBE_PROXY.trim();
  try {
    const m = fs.readFileSync(path.join(ROOT, '.env'), 'utf8').match(/^\s*YOUTUBE_PROXY\s*=\s*["']?([^"'\r\n]+)/m);
    return m ? m[1].trim() : '';
  } catch {
    return '';
  }
}

function toNetscape(cookies) {
  const lines = ['# Netscape HTTP Cookie File', MARKER, ''];
  for (const c of cookies) {
    const sub = c.domain.startsWith('.');
    lines.push([c.domain, sub ? 'TRUE' : 'FALSE', c.path, c.secure ? 'TRUE' : 'FALSE',
      c.expires > 0 ? Math.floor(c.expires) : 0, c.name, c.value].join('\t'));
  }
  return lines.join('\n') + '\n';
}

async function refresh() {
  const proxy = readProxy();
  const context = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: true,
    proxy: proxy ? { server: proxy } : undefined,
    locale: 'en-US',
    args: ['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-gpu',
      '--disable-dev-shm-usage', '--mute-audio', '--js-flags=--max-old-space-size=256'],
  });
  try {
    // The server has ~1GB RAM shared with the bot: skip everything that is not
    // needed for YouTube to set its cookies.
    await context.route('**/*', (route) =>
      ['image', 'media', 'font', 'stylesheet'].includes(route.request().resourceType())
        ? route.abort()
        : route.continue());
    const page = context.pages()[0] || (await context.newPage());
    await page.goto('https://www.youtube.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(5000); // let YouTube set/rotate its cookies
    const cookies = (await context.cookies()).filter((c) => /(^|\.)(youtube|google)\.com$/.test(c.domain));
    if (!cookies.length) throw new Error('YouTube set no cookies');

    const tmp = COOKIE_FILE + '.tmp';
    fs.writeFileSync(tmp, toNetscape(cookies), { mode: 0o600 });
    fs.renameSync(tmp, COOKIE_FILE); // replaces the previous cookies.txt atomically
    const loggedIn = cookies.some((c) => c.name === 'SAPISID' || c.name === '__Secure-1PSID');
    log(`Refreshed ${cookies.length} cookies (${loggedIn ? 'signed-in' : 'anonymous visitor'}) via ${proxy || 'direct'}.`);
  } finally {
    await context.close();
  }
}

log(`dcBot cookie refresher started; refreshing every ${REFRESH_HOURS}h.`);
for (;;) {
  try {
    await refresh();
  } catch (err) {
    log(`Refresh failed: ${err.message}`);
  }
  await new Promise((r) => setTimeout(r, REFRESH_HOURS * 3600 * 1000));
}
