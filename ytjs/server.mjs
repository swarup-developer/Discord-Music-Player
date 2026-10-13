// Local YouTube / YouTube Music resolver for dcBot.
//
// Keeps one warm InnerTube session (youtubei.js) alive for the life of the
// process, so search and stream resolution cost a single API round trip each
// instead of the multi-second cold start yt-dlp pays on every extraction.
//
// Endpoints (all GET, JSON):
//   /health
//   /stream?id=<videoId>            -> { url, title, duration, isLive, client }
//   /search?q=<text>&limit=N&music=1 -> { results: [...] }  (music=1: YouTube Music songs)
//
// SECURITY: binds to 127.0.0.1 only. Exposing this on a public interface would
// make it an open, unauthenticated YouTube extraction proxy.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { Innertube, UniversalCache, Platform, Log } from 'youtubei.js';

const HOST = '127.0.0.1';
const PORT = Number(process.env.YTJS_PORT || 4417);
const HERE = path.dirname(fileURLToPath(import.meta.url));

// Route YouTube traffic through YOUTUBE_PROXY from the bot's .env (e.g.
// Cloudflare WARP), the same proxy FFmpeg uses -- stream URLs are bound to the
// resolving IP. Node's built-in fetch only honours HTTPS_PROXY when
// NODE_USE_ENV_PROXY is set at startup, so re-launch once with it set.
function readEnvProxy() {
  if (process.env.YOUTUBE_PROXY) return process.env.YOUTUBE_PROXY.trim();
  try {
    const m = fs.readFileSync(path.join(HERE, '..', '.env'), 'utf8').match(/^\s*YOUTUBE_PROXY\s*=\s*["']?([^"'\r\n]+)/m);
    return m ? m[1].trim() : '';
  } catch {
    return '';
  }
}
const PROXY = readEnvProxy();
if (PROXY && process.env.NODE_USE_ENV_PROXY !== '1') {
  const { spawn } = await import('node:child_process');
  const child = spawn(process.execPath, process.argv.slice(1), {
    stdio: 'inherit',
    env: { ...process.env, NODE_USE_ENV_PROXY: '1', HTTPS_PROXY: PROXY, HTTP_PROXY: PROXY, NO_PROXY: '127.0.0.1,localhost' },
  });
  for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => child.kill(sig));
  child.on('exit', (code) => process.exit(code ?? 1));
  await new Promise(() => {}); // the child is the real server
}

// Unknown-renderer parser warnings are noise for a resolver.
Log.setLevel(Log.Level.ERROR);

// youtubei.js >= 16 ships no JS evaluator; without one every client whose
// formats need signature/n deciphering (WEB, MWEB, YTMUSIC) fails with
// "you must provide your own JavaScript evaluator". Run YouTube's player
// snippet in an isolated vm context with a hard timeout -- the same thing
// yt-dlp does with its node/ejs runtime. The snippet already ends in
// `return process(...)`, so it only needs a function wrapper.
Platform.shim.eval = (data) =>
  vm.runInNewContext(`(function(){${data.output}\n})()`, { URL, URLSearchParams }, { timeout: 2000 });

// Resolved googlevideo URLs stay valid for hours, so a short cache spares a
// full InnerTube round trip when a track is replayed, seeked or re-queued.
const CACHE_TTL_MS = 30 * 60 * 1000;
const cache = new Map();
// Concurrent requests for the same video share one resolution.
const inflight = new Map();

// Measured (~250ms each with a warm session): YTMUSIC and MWEB return playable
// URLs through the evaluator above. Without a PO token, audio-only itag 140 is
// often cut off after the first ~1MB (HTTP 403 deeper in the file), while the
// muxed itag 18 plays fully -- the same format yt-dlp ends up with, but ~20x
// faster. IOS is last: its audio URLs hit the same 403 and it has no itag 18.
const CLIENTS = ['YTMUSIC', 'MWEB', 'WEB', 'IOS'];
const FORMAT_TYPES = ['audio', 'video+audio'];

/**
 * Check the URL serves bytes well past the start of the file. A 0-1023 probe
 * is not enough: PO-token-gated URLs answer the first range and 403 the rest,
 * which plays a second of audio and then goes silent.
 */
async function isFullyPlayable(url) {
  try {
    const clen = Number(new URL(url).searchParams.get('clen')) || 0;
    const start = clen > 4096 ? Math.floor(clen * 0.6) : 0;
    const res = await fetch(url, {
      headers: { Range: `bytes=${start}-${start + 1023}` },
      signal: AbortSignal.timeout(5000),
    });
    await res.body?.cancel();
    return res.status === 200 || res.status === 206;
  } catch {
    return false;
  }
}

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

/**
 * Build a Cookie header from the bot's Netscape cookies.txt, if present.
 * Only YouTube/Google domains are included. Values are never logged.
 */
function loadCookieHeader() {
  for (const candidate of COOKIE_FILES) {
    try {
      const text = fs.readFileSync(candidate, 'utf8');
      const pairs = [];
      for (const line of text.split(/\r?\n/)) {
        if (!line || line.startsWith('#')) continue;
        const f = line.split('\t');
        if (f.length < 7) continue;
        const domain = f[0].toLowerCase();
        if (!domain.includes('youtube.com') && !domain.includes('google.com')) continue;
        pairs.push(`${f[5]}=${f[6]}`);
      }
      // A signed-out / rotated-away export only makes YouTube more suspicious;
      // the bot retires such files, and here they are simply not sent.
      if (pairs.length && !pairs.some((p) => /^(SAPISID|__Secure-1PSID)=/.test(p))) {
        // Anonymous visitor cookies (from the refresher): the locally
        // generated session is equivalent, so nothing to send.
        continue;
      }
      if (pairs.length) {
        return pairs.join('; ');
      }
    } catch {
      // Not there, or unreadable -- anonymous extraction still works.
    }
  }
  return undefined;
}

// Rebuild the session whenever cookies.txt changes, so dropping in fresh
// cookies takes effect immediately without restarting the resolver.
const COOKIE_FILES = [path.join(HERE, '..', 'cookies.txt'), path.join(HERE, '..', 'bot', 'cookies.txt')];
const cookieStamp = () =>
  COOKIE_FILES.map((f) => { try { return fs.statSync(f).mtimeMs; } catch { return 0; } }).join(':');
let sessionCookieStamp = null;
let sessionCookieHeader;

let innertubePromise = null;
function getInnertube() {
  const stamp = cookieStamp();
  if (innertubePromise && stamp !== sessionCookieStamp) {
    sessionCookieStamp = stamp;
    // The refresher rewrites cookies.txt every few hours; only rebuild (and
    // drop the URL cache) when the cookies actually sent to YouTube change.
    if (loadCookieHeader() !== sessionCookieHeader) {
      log('Login cookies changed; rebuilding InnerTube session.');
      innertubePromise = null;
      cache.clear();
    }
  }
  sessionCookieStamp = stamp;
  if (!innertubePromise) {
    sessionCookieHeader = loadCookieHeader();
    innertubePromise = Innertube.create({
      cache: new UniversalCache(true, path.join(HERE, '.cache')),
      cookie: sessionCookieHeader,
      generate_session_locally: true,
    }).then((yt) => {
      log(`InnerTube session: ${sessionCookieHeader ? 'signed-in cookies' : 'anonymous'}.`);
      return yt;
    }).catch((err) => {
      innertubePromise = null;
      throw err;
    });
  }
  return innertubePromise;
}

/** Return the first fully playable URL: audio-only first, then muxed itag 18. */
async function pickAudioUrl(yt, info) {
  for (const type of FORMAT_TYPES) {
    let format;
    try {
      format = info.chooseFormat({ type, quality: 'best' });
    } catch {
      continue; // "No matching formats found"
    }
    let url;
    try {
      url = await format.decipher(yt.session.player);
    } catch {
      url = format.url; // already-plain URL ("No valid URL to decipher")
    }
    if (url && (await isFullyPlayable(url))) return url;
  }
  return null;
}

/**
 * Resolve a video ID to { url, title, duration, isLive }.
 * Each client is tried in turn; a video blocked for one is often fine on another.
 */
async function resolve(videoId) {
  const yt = await getInnertube();
  let lastError = null;

  for (const client of CLIENTS) {
    try {
      // getBasicInfo is one /player call; getInfo adds a /next call we never use.
      const info = await yt.getBasicInfo(videoId, { client });
      const details = info.basic_info || {};
      const status = info.playability_status?.status;
      if (status && status !== 'OK') {
        lastError = new Error(`${status}: ${info.playability_status?.reason || 'unplayable'}`);
        continue;
      }
      const isLive = Boolean(details.is_live);

      if (isLive) {
        const manifest = info.streaming_data?.hls_manifest_url || info.streaming_data?.dash_manifest_url;
        if (manifest) {
          return { url: manifest, title: details.title || 'Unknown YouTube Video', duration: 0, isLive: true, client };
        }
        lastError = new Error('live stream has no manifest URL');
        continue;
      }

      const url = await pickAudioUrl(yt, info);
      if (!url) {
        lastError = new Error('no fully playable format');
        continue;
      }
      return {
        url,
        title: details.title || 'Unknown YouTube Video',
        duration: Number(details.duration || 0),
        isLive: false,
        client,
      };
    } catch (err) {
      lastError = err;
      log(`client ${client} failed for ${videoId}: ${err.message}`);
    }
  }

  throw lastError || new Error('all clients failed');
}

async function resolveCached(videoId) {
  const hit = cache.get(videoId);
  if (hit && hit.expires > Date.now()) return hit.value;
  if (inflight.has(videoId)) return inflight.get(videoId);

  const p = resolve(videoId)
    .then((value) => {
      cache.set(videoId, { value, expires: Date.now() + CACHE_TTL_MS });
      // Bound the cache so a long-running process cannot grow without limit.
      if (cache.size > 256) cache.delete(cache.keys().next().value);
      return value;
    })
    .finally(() => inflight.delete(videoId));
  inflight.set(videoId, p);
  return p;
}

const text = (v) => (v == null ? '' : typeof v === 'string' ? v : v.toString());

/** YouTube Music "songs" search: official audio, no video intros/outros. */
async function searchMusic(yt, q, limit) {
  const res = await yt.music.search(q, { type: 'song' });
  const items = res.songs?.contents || res.contents?.flatMap((s) => s.contents || []) || [];
  return items
    .filter((s) => s?.id)
    .slice(0, limit)
    .map((s) => {
      const artist = (s.artists || []).map((a) => a.name).join(', ') || s.author?.name || 'YouTube Music';
      return {
        id: s.id,
        title: s.title ? `${s.title} - ${artist}` : 'Unknown',
        song: text(s.title) || 'Unknown',
        album: s.album?.name || artist,
        artist,
        duration: s.duration?.seconds || 0,
        url: `https://music.youtube.com/watch?v=${s.id}`,
        provider: 'youtube',
      };
    });
}

async function searchVideos(yt, q, limit) {
  const res = await yt.search(q, { type: 'video' });
  return (res.videos || [])
    .filter((v) => v?.id)
    .slice(0, limit)
    .map((v) => ({
      id: v.id,
      title: text(v.title) || 'Unknown YouTube Video',
      song: text(v.title) || 'Unknown YouTube Video',
      album: v.author?.name || 'YouTube',
      duration: v.duration?.seconds || 0,
      url: `https://www.youtube.com/watch?v=${v.id}`,
      provider: 'youtube',
    }));
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  const started = Date.now();

  if (url.pathname === '/health') {
    sendJson(res, 200, { ok: true, cached: cache.size });
    return;
  }

  if (url.pathname === '/search') {
    const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
    const limit = Math.min(Math.max(Number(url.searchParams.get('limit')) || 10, 1), 25);
    const music = url.searchParams.get('music') !== '0';
    if (!q) {
      sendJson(res, 400, { error: 'q is required' });
      return;
    }
    try {
      const yt = await getInnertube();
      let results = music ? await searchMusic(yt, q, limit).catch((e) => {
        log(`music search failed for "${q}": ${e.message}`);
        return [];
      }) : [];
      if (!results.length) results = await searchVideos(yt, q, limit);
      log(`search "${q}" (${music ? 'music' : 'video'}) -> ${results.length} in ${Date.now() - started}ms`);
      sendJson(res, 200, { results });
    } catch (err) {
      log(`search failed for "${q}": ${err.message}`);
      sendJson(res, 502, { error: err.message });
    }
    return;
  }

  if (url.pathname !== '/stream') {
    sendJson(res, 404, { error: 'not found' });
    return;
  }

  const videoId = url.searchParams.get('id') || '';
  if (!VIDEO_ID.test(videoId)) {
    sendJson(res, 400, { error: 'id must be an 11-character YouTube video ID' });
    return;
  }

  try {
    const result = await resolveCached(videoId);
    log(`resolved ${videoId} via ${result.client} in ${Date.now() - started}ms`);
    sendJson(res, 200, result);
  } catch (err) {
    log(`failed ${videoId} after ${Date.now() - started}ms: ${err.message}`);
    sendJson(res, 502, { error: err.message });
  }
});

server.listen(PORT, HOST, () => {
  log(`dcBot YouTube resolver listening on http://${HOST}:${PORT} (proxy: ${PROXY || 'none'})`);
  // Warm the session now so the first /play does not pay the ~3s cold start.
  getInnertube().then(() => log('InnerTube session ready.')).catch((e) => log(`warm-up failed: ${e.message}`));
});
