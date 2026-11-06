"""Spotify playback through Spotify Soloist, Spotify's official headless player.

Each user logs in once with their own Soloist API key (from
developer.spotify.com/dashboard/soloist). The bot then runs one Soloist
process per active user. Soloist plays into a private PulseAudio "null sink";
FFmpeg records that sink's monitor and Discord plays it. Control (play a URI,
pause, next, seek, volume, queue) goes through Soloist's local WebSocket API.

Search (song names) uses the bot owner's Spotify app through the Web API
(SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env). Spotify links always work,
even without it.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Optional

import httpx

from .spotify_connect import (
    ROOT,
    SESSIONS_DIR,
    PremiumRequired,
    SpotifyError,
    UserStore,
    _track_info,
)

logger = logging.getLogger(__name__)

MAX_SESSIONS = int(os.getenv("SPOTIFY_MAX_SESSIONS", "2"))
IDLE_SECONDS = int(os.getenv("SPOTIFY_IDLE_MINUTES", "5")) * 60
BASE_PORT = int(os.getenv("SPOTIFY_BASE_PORT", "4560"))
DEVICE_NAME = os.getenv("SPOTIFY_DEVICE_NAME", "Discord Music Bot")
KEY_PAGE = "https://developer.spotify.com/dashboard/soloist"

PULSE_DIR = SESSIONS_DIR / "pulse"
# Everything audio-related in the bot process talks to our private PulseAudio.
PULSE_SERVER = f"unix:{PULSE_DIR / 'native'}"

_KEY_RE = re.compile(r"^spak_[A-Za-z0-9_\-]{16,}$")


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key.strip()))


def find_soloist() -> Optional[str]:
    for c in (os.getenv("SOLOIST_BIN", ""), "/usr/local/bin/soloist", shutil.which("soloist") or ""):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _env(extra: Optional[dict] = None) -> dict:
    env = {**os.environ, "PULSE_SERVER": PULSE_SERVER, "PULSE_RUNTIME_PATH": str(PULSE_DIR),
           "XDG_RUNTIME_DIR": str(PULSE_DIR), "HOME": os.environ.get("HOME") or str(ROOT.parent)}
    if extra:
        env.update(extra)
    return env


def _redact(text: str) -> str:
    return re.sub(r"spak_[A-Za-z0-9_\-]+", "spak_***", text)


# --------------------------------------------------------------- pulse ----

_pulse_lock = asyncio.Lock()


async def _run(*args: str, env: Optional[dict] = None, timeout: float = 15) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env or _env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "timeout"
    return proc.returncode, out.decode("utf-8", "replace")


async def ensure_pulse() -> None:
    """Start a private PulseAudio for the bot if it isn't running."""
    async with _pulse_lock:
        PULSE_DIR.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(PULSE_DIR, 0o700)
        rc, _ = await _run("pactl", "info")
        if rc == 0:
            return
        if not shutil.which("pulseaudio"):
            raise SpotifyError("Spotify playback isn't installed on this bot's server yet (PulseAudio missing).")
        rc, out = await _run(
            "pulseaudio", "--daemonize=yes", "--exit-idle-time=-1", "--disallow-exit",
            "--disable-shm=yes", "-n",
            "--load=module-native-protocol-unix auth-anonymous=1",
            "--load=module-null-sink sink_name=idle",
        )
        for _ in range(20):
            await asyncio.sleep(0.25)
            if (await _run("pactl", "info"))[0] == 0:
                logger.info("Started private PulseAudio for Spotify")
                return
        logger.error("PulseAudio failed to start: %s", out[-500:])
        raise SpotifyError("The bot's audio system for Spotify could not start.")


async def make_sink(name: str) -> None:
    await ensure_pulse()
    rc, out = await _run("pactl", "list", "short", "sinks")
    if name not in out:
        await _run("pactl", "load-module", "module-null-sink", f"sink_name={name}",
                   "rate=48000", "channels=2", "format=s16le")


async def drop_sink(name: str) -> None:
    rc, out = await _run("pactl", "list", "short", "modules")
    for line in out.splitlines():
        if f"sink_name={name}" in line:
            await _run("pactl", "unload-module", line.split()[0])


def ffmpeg_input(sink: str) -> tuple[str, str]:
    """(input, before_options) for discord.FFmpegPCMAudio to record a sink."""
    return f"{sink}.monitor", "-nostdin -f pulse -sample_rate 48000 -channels 2 -fragment_size 3840"


# --------------------------------------------------------------- search ---

class Search:
    """Spotify Web API search with the bot owner's app (client credentials)."""

    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        self._token: Optional[tuple[str, float]] = None

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _bearer(self) -> str:
        if self._token and self._token[1] > time.time():
            return self._token[0]
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://accounts.spotify.com/api/token",
                             data={"grant_type": "client_credentials"},
                             headers={"Authorization": f"Basic {auth}"})
        if r.status_code != 200:
            logger.error("Spotify search login failed: HTTP %s", r.status_code)
            raise SpotifyError("Spotify search isn't working right now. Spotify links still work.")
        tok = r.json()
        self._token = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        return tok["access_token"]

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.available:
            raise SpotifyError("Song search isn't set up on this bot yet. Paste a Spotify song, album or playlist link instead.")
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"https://api.spotify.com/v1{path}", params=params,
                            headers={"Authorization": f"Bearer {await self._bearer()}"})
        if r.status_code == 429:
            raise SpotifyError("Spotify is busy. Try again in a minute.")
        if r.status_code != 200:
            raise SpotifyError(f"Spotify search answered HTTP {r.status_code}.")
        return r.json()

    async def track(self, query: str) -> Optional[dict]:
        data = await self._get("/search", {"q": query, "type": "track", "limit": 1})
        items = (data.get("tracks") or {}).get("items") or []
        return _track_info(items[0]) if items else None

    async def track_by_id(self, track_id: str) -> Optional[dict]:
        if not self.available:
            return None
        with contextlib.suppress(SpotifyError):
            return _track_info(await self._get(f"/tracks/{track_id}"))
        return None


# ------------------------------------------------------------- session ----

class SoloistSession:
    def __init__(self, user_id: int, guild_id: int, port: int):
        self.user_id = user_id
        self.guild_id = guild_id
        self.port = port
        self.dir = SESSIONS_DIR / f"soloist-{user_id}"
        self.sink = f"spotify_{user_id}"
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.ws = None
        self.last_activity = time.time()
        self.paused = False
        self.now_playing: dict = {}
        self.position_ms = 0
        self.position_at = time.time()
        self.on_event = None
        self._tasks: list[asyncio.Task] = []
        self._log_tail: list[str] = []
        self.ready = asyncio.Event()

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)

    async def start(self, binary: str, key: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)
        await make_sink(self.sink)
        self.proc = await asyncio.create_subprocess_exec(
            binary, "-n", DEVICE_NAME, "-k", key, "-D", str(self.dir),
            "-C", str(self.dir / "cache"), "-z", "100", "-i", "100",
            "-w", f"127.0.0.1:{self.port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=_env({"PULSE_SINK": self.sink}), start_new_session=True,
        )
        self._tasks.append(asyncio.create_task(self._read_logs()))
        import websockets
        deadline = time.time() + 30
        while time.time() < deadline and self.alive:
            try:
                self.ws = await websockets.connect(f"ws://127.0.0.1:{self.port}", max_size=2**20)
                break
            except OSError:
                await asyncio.sleep(0.5)
        if not self.ws:
            tail = _redact("\n".join(self._log_tail[-15:]))
            await self.stop()
            logger.error("Soloist failed to start:\n%s", tail)
            if "premium" in tail.lower():
                raise PremiumRequired()
            raise SpotifyError("The Spotify player could not start. Try `/spotify login` again.")
        self._tasks.append(asyncio.create_task(self._read_events()))
        await self.send("get_auth_state")
        await self.send("get_state")

    async def _read_logs(self):
        while self.proc and self.proc.stdout:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = _redact(line.decode("utf-8", "replace").rstrip())
            self._log_tail.append(text)
            del self._log_tail[:-60]
            if any(w in text.lower() for w in ("error", "warn", "fail")):
                logger.info("soloist[%s]: %s", self.user_id, text[:300])

    async def _read_events(self):
        seen = 0
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if seen < 8:  # learn the payload shapes in the log
                    seen += 1
                    logger.info("soloist event: %s", _redact(str(msg))[:400])
                self._handle(msg)
        except Exception:
            pass

    def _handle(self, msg: dict):
        etype = msg.get("event") or msg.get("type") or ""
        data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
        flat = json.dumps(data).lower()
        if "logged_in" in flat or "authenticated" in flat:
            self.ready.set()
        track = data.get("track") if isinstance(data.get("track"), dict) else None
        if etype in ("track_changed",) or track:
            t = track or data
            artists = t.get("artists") or t.get("artist_names") or t.get("artist") or []
            if isinstance(artists, list):
                artists = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in artists)
            images = t.get("images") or t.get("album_images") or []
            image = t.get("image_url") or t.get("cover_url") or t.get("image") or (
                images[0].get("url") if images and isinstance(images[0], dict) else None)
            self.now_playing = {
                "name": t.get("name") or t.get("title") or "Spotify",
                "artists": artists,
                "duration_ms": t.get("duration_ms") or t.get("duration") or 0,
                "image": image,
                "uri": t.get("uri"),
            }
            self.position_ms, self.position_at = 0, time.time()
        for key in ("position_ms", "position"):
            if isinstance(data.get(key), (int, float)):
                self.position_ms, self.position_at = int(data[key]), time.time()
        for key in ("is_paused", "paused"):
            if isinstance(data.get(key), bool):
                self.paused = data[key]
        if isinstance(data.get("is_playing"), bool):
            self.paused = not data["is_playing"]
        state = str(data.get("state") or data.get("playback_state") or "").lower()
        if state in ("paused", "stopped", "idle"):
            self.paused = True
        elif state == "playing":
            self.paused = False
            self.last_activity = time.time()
        if self.on_event:
            with contextlib.suppress(Exception):
                self.on_event(self, etype, data)

    async def send(self, command: str, **fields):
        if not self.ws:
            raise SpotifyError("The Spotify player isn't running.")
        self.last_activity = time.time()
        await self.ws.send(json.dumps({"type": "command", "command": command, **fields}))

    def current_position(self) -> int:
        if self.paused:
            return self.position_ms
        return self.position_ms + int((time.time() - self.position_at) * 1000)

    async def play(self, uri: str):
        await self.send("activate")
        await asyncio.sleep(0.3)
        await self.send("play", uri=uri)
        self.paused = False

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.ws:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None
        with contextlib.suppress(Exception):
            await drop_sink(self.sink)


class SoloistManager:
    def __init__(self, store: UserStore):
        self.store = store
        self.search = Search()
        self.by_guild: dict[int, SoloistSession] = {}
        self._lock = asyncio.Lock()
        self._busy_ports: set[int] = set()

    def key_for(self, user_id: int) -> Optional[str]:
        data = self.store.get(user_id) or {}
        return data.get("soloist_key")

    def logged_in(self, user_id: int) -> bool:
        return bool(self.key_for(user_id))

    def get(self, guild_id: int) -> Optional[SoloistSession]:
        s = self.by_guild.get(guild_id)
        return s if s and s.alive else None

    def _free_port(self) -> int:
        used = {s.port for s in self.by_guild.values()} | self._busy_ports
        return next(p for p in range(BASE_PORT, BASE_PORT + 50) if p not in used)

    async def login(self, user_id: int, key: str, on_step) -> None:
        """Save the key and pair once (Soloist --pair). on_step(text, url) reports
        any link or code Soloist asks the user to open."""
        binary = find_soloist()
        if not binary:
            raise SpotifyError("Spotify playback isn't installed on this bot's server yet.")
        await self.stop_user(user_id)
        await ensure_pulse()
        session_dir = SESSIONS_DIR / f"soloist-{user_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(session_dir, 0o700)
        proc = await asyncio.create_subprocess_exec(
            binary, "-n", DEVICE_NAME, "-k", key.strip(), "-D", str(session_dir), "--pair", "-v",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=_env({"PULSE_SINK": "idle"}), start_new_session=True,
        )
        tail: list[str] = []
        reported: set[str] = set()

        async def reader():
            while proc.stdout:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = _redact(line.decode("utf-8", "replace").rstrip())
                tail.append(text)
                logger.info("soloist pair[%s]: %s", user_id, text[:300])
                url = next(iter(re.findall(r"https?://\S+", text)), None)
                if url and url not in reported:
                    reported.add(url)
                    with contextlib.suppress(Exception):
                        await on_step(text, url.rstrip(".,)"))

        read_task = asyncio.create_task(reader())
        try:
            rc = await asyncio.wait_for(proc.wait(), 600)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise SpotifyError("Login wasn't finished in time. Run `/spotify login` again.")
        finally:
            await asyncio.sleep(0.2)
            read_task.cancel()
        text = "\n".join(tail).lower()
        if rc != 0:
            if "premium" in text:
                raise PremiumRequired()
            if "key" in text and ("invalid" in text or "unauthor" in text or "denied" in text):
                raise SpotifyError("Spotify didn't accept that key. Copy it again from the Soloist page.")
            if rc == 10:
                raise SpotifyError("The Spotify player on this server is out of date. Ask the bot owner to update Soloist.")
            logger.error("Soloist pairing failed (rc=%s):\n%s", rc, "\n".join(tail[-20:]))
            raise SpotifyError("Spotify login didn't finish. Run `/spotify login` and try again.")
        data = self.store.get(user_id) or {}
        data["soloist_key"] = key.strip()
        self.store.put(user_id, data)

    async def ensure(self, guild_id: int, user_id: int) -> SoloistSession:
        async with self._lock:
            current = self.by_guild.get(guild_id)
            if current and current.alive and current.user_id == user_id:
                return current
            if current:
                await current.stop()
                self.by_guild.pop(guild_id, None)
            for gid, s in list(self.by_guild.items()):
                if s.user_id == user_id:
                    await s.stop()
                    self.by_guild.pop(gid, None)
            if len([s for s in self.by_guild.values() if s.alive]) >= MAX_SESSIONS:
                raise SpotifyError("Spotify is already playing in the maximum number of servers. Try again later.")
            binary = find_soloist()
            key = self.key_for(user_id)
            if not binary:
                raise SpotifyError("Spotify playback isn't installed on this bot's server yet.")
            if not key:
                raise SpotifyError("Log in first with `/spotify login`.")
            session = SoloistSession(user_id, guild_id, self._free_port())
            await session.start(binary, key)
            self.by_guild[guild_id] = session
            logger.info("Started Soloist for guild %s on port %s", guild_id, session.port)
            return session

    async def stop(self, guild_id: int):
        async with self._lock:
            s = self.by_guild.pop(guild_id, None)
        if s:
            await s.stop()
            logger.info("Stopped Soloist for guild %s", guild_id)

    async def stop_user(self, user_id: int):
        for gid, s in list(self.by_guild.items()):
            if s.user_id == user_id:
                await self.stop(gid)

    async def stop_all(self):
        for gid in list(self.by_guild):
            await self.stop(gid)

    def forget(self, user_id: int) -> bool:
        shutil.rmtree(SESSIONS_DIR / f"soloist-{user_id}", ignore_errors=True)
        shutil.rmtree(SESSIONS_DIR / str(user_id), ignore_errors=True)
        return self.store.delete(user_id)
