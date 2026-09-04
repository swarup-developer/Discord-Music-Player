// Local YouTube stream resolver for dcBot.
//
// Replaces the public Invidious and Cobalt instances the bot used to fall back
// to. Every one of those was dead, rate-limited or returning empty tunnels, so
// the "fallback" reliably failed exactly when yt-dlp had already failed. This
// resolves streams in-process with youtubei.js instead, using the same InnerTube
// API the YouTube clients use, so it depends on no third-party uptime.
//
// SECURITY: binds to 127.0.0.1 only. Exposing this on a public interface would
// make it an open, unauthenticated YouTube extraction proxy.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Innertube, UniversalCache } from 'youtubei.js';

const HOST = '127.0.0.1';
const PORT = Number(process.env.YTJS_PORT || 4417);
const HERE = path.dirname(fileURLToPath(import.meta.url));

// Resolved googlevideo URLs stay valid for hours, so a short cache spares a
// full InnerTube round trip when a track is replayed, seeked or re-queued.
const CACHE_TTL_MS = 30 * 60 * 1000;
const cache = new Map();

// InnerTube clients to try in order. ANDROID and IOS return unciphered URLs and
// are the least likely to be challenged; WEB is the fallback that supports the
// widest set of videos but needs the player to decipher formats.
const CLIENTS = ['ANDROID', 'IOS', 'WEB'];

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

/**
 * Build a Cookie header from the bot's Netscape cookies.txt, if present.
 * Only YouTube/Google domains are included. Values are never logged.
 */
function loadCookieHeader() {
  for (const candidate of [
    path.join(HERE, '..', 'cookies.txt'),
    path.join(HERE, '..', 'bot', 'cookies.txt'),
  ]) {
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
      if (pairs.length) {
        log(`Loaded ${pairs.length} cookies from ${candidate}`);
        return pairs.join('; ');
      }
    } catch {
      // Not there, or unreadable -- anonymous extraction still works.
    }
  }
  log('No cookies.txt found; resolving anonymously.');
  return undefined;
}

let innertubePromise = null;
function getInnertube() {
  if (!innertubePromise) {
    innertubePromise = Innertube.create({
      cache: new UniversalCache(false),
      cookie: loadCookieHeader(),
      generate_session_locally: true,
    }).catch((err) => {
      innertubePromise = null;
      throw err;
    });
  }
  return innertubePromise;
}

/** Pick the best audio-only format and return a playable URL. */
async function pickAudioUrl(yt, info) {
  const format = info.chooseFormat({ type: 'audio', quality: 'best' });
  if (!format) return null;
  // youtubei.js now exposes already-deciphered URLs for many clients. Calling
  // decipher() on those formats throws "No valid URL to decipher" in v18.
  if (format.url) return format.url;
  if (typeof format.decipher === 'function') {
    return format.decipher(yt.session.player);
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
      const info = await yt.getInfo(videoId, client);
      const details = info.basic_info || {};
      const isLive = Boolean(details.is_live);

      if (isLive) {
        const manifest =
          info.streaming_data?.hls_manifest_url || info.streaming_data?.dash_manifest_url;
        if (manifest) {
          return {
            url: manifest,
            title: details.title || 'Unknown YouTube Video',
            duration: 0,
            isLive: true,
            client,
          };
        }
        lastError = new Error('live stream has no manifest URL');
        continue;
      }

      const url = await pickAudioUrl(yt, info);
      if (!url) {
        lastError = new Error('no audio format available');
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

  const value = await resolve(videoId);
  cache.set(videoId, { value, expires: Date.now() + CACHE_TTL_MS });
  // Bound the cache so a long-running process cannot grow without limit.
  if (cache.size > 256) cache.delete(cache.keys().next().value);
  return value;
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

  if (url.pathname === '/health') {
    sendJson(res, 200, { ok: true, cached: cache.size });
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

  const started = Date.now();
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
  log(`dcBot YouTube resolver listening on http://${HOST}:${PORT}`);
});
