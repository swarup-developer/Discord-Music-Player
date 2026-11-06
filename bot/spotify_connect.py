"""Real Spotify playback (Premium only), with no YouTube involved.

How it works
------------
* Every user brings their own Spotify app (Client ID) from
  developer.spotify.com and logs in once with OAuth (PKCE, so no client
  secret). Their refresh token is stored encrypted in spotify_users.json.
* When they play something, the bot starts go-librespot, an open-source
  Spotify Connect player, logged in as that user. It writes raw PCM audio
  (s16le, 44.1 kHz, stereo) into a FIFO, which FFmpeg reads and Discord plays.
* go-librespot plays Spotify URIs (tracks, albums, playlists) itself, so the
  Web API is only used for search and to learn the account's username.
* Sessions stop by themselves after a few idle minutes, so nothing stays in
  RAM when nobody is listening.

Spotify Premium is required by go-librespot and, since February 2026, by
Spotify for every Development Mode app.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT / "spotify_users.json"
KEY_FILE = ROOT / "spotify_secret.key"
SESSIONS_DIR = ROOT / "spotify_sessions"

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = " ".join([
    "streaming",
    "user-read-private",
    "user-read-email",
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
])
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

MAX_SESSIONS = int(os.getenv("SPOTIFY_MAX_SESSIONS", "2"))
IDLE_SECONDS = int(os.getenv("SPOTIFY_IDLE_MINUTES", "5")) * 60
BASE_PORT = int(os.getenv("SPOTIFY_BASE_PORT", "4520"))

# Raw audio go-librespot writes into the FIFO.
PCM_BEFORE_OPTIONS = "-nostdin -f s16le -ar 44100 -ac 2"

_CLIENT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]{22})"
)
_URI_RE = re.compile(r"^spotify:(track|album|playlist|artist|episode|show):([A-Za-z0-9]{22})$")


class SpotifyError(Exception):
    """A problem worth showing to the user as-is."""


class AudioRefused(SpotifyError):
    def __init__(self):
        super().__init__(
            "Spotify refused to send this account's audio to the bot, so nothing can play. "
            "This is a block on Spotify's side that affects many accounts (mostly newer ones) "
            "with every non-official Spotify player; the official Spotify app still works. "
            "There's no fix on the bot's side. Pick another source with `/provider` "
            "(YouTube Music is the default), or try a different (older) Spotify Premium account."
        )


class PremiumRequired(SpotifyError):
    def __init__(self):
        super().__init__(
            "Spotify playback needs a Spotify Premium account. "
            "You can still play music from YouTube or JioSaavn with /play."
        )


def find_librespot() -> Optional[str]:
    for candidate in (
        os.getenv("GO_LIBRESPOT_BIN", ""),
        str(ROOT.parent / "go-librespot" / "go-librespot"),
        str(Path.home() / "go-librespot" / "go-librespot"),
        shutil.which("go-librespot") or "",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_spotify_link(text: str) -> Optional[str]:
    """Return a spotify:<type>:<id> URI for a Spotify link or URI, else None."""
    text = text.strip()
    m = _URI_RE.match(text)
    if m:
        return text
    m = _LINK_RE.search(text)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return None


def valid_client_id(client_id: str) -> bool:
    return bool(_CLIENT_ID_RE.match(client_id.strip()))


# ------------------------------------------------------------ encryption ---

def _load_key() -> bytes:
    env = os.getenv("SPOTIFY_TOKEN_KEY", "").strip()
    if env:
        key = base64.urlsafe_b64decode(env + "=" * (-len(env) % 4))
        if len(key) == 32:
            return key
        logger.warning("SPOTIFY_TOKEN_KEY is not 32 bytes of base64; using %s", KEY_FILE.name)
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = os.urandom(32)
    KEY_FILE.write_bytes(key)
    with contextlib.suppress(OSError):
        os.chmod(KEY_FILE, 0o600)
    return key


def _encrypt(data: dict) -> str:
    from Crypto.Cipher import AES  # pycryptodome

    cipher = AES.new(_load_key(), AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(json.dumps(data).encode())
    return base64.b64encode(cipher.nonce + tag + ct).decode()


def _decrypt(blob: str) -> dict:
    from Crypto.Cipher import AES

    raw = base64.b64decode(blob)
    nonce, tag, ct = raw[:16], raw[16:32], raw[32:]
    cipher = AES.new(_load_key(), AES.MODE_GCM, nonce=nonce)
    return json.loads(cipher.decrypt_and_verify(ct, tag))


# ------------------------------------------------------------ user store ---

class UserStore:
    """Encrypted per-user Spotify logins: client_id, refresh_token, username."""

    def __init__(self):
        self._users: dict[str, str] = {}
        if USERS_FILE.exists():
            try:
                self._users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.warning("Could not read %s: %s", USERS_FILE.name, e)

    def _save(self):
        tmp = USERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._users), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, USERS_FILE)

    def get(self, user_id: int) -> Optional[dict]:
        blob = self._users.get(str(user_id))
        if not blob:
            return None
        try:
            return _decrypt(blob)
        except Exception:
            logger.warning("Stored Spotify login for a user could not be decrypted; ignoring it")
            return None

    def put(self, user_id: int, data: dict):
        self._users[str(user_id)] = _encrypt(data)
        self._save()

    def delete(self, user_id: int) -> bool:
        existed = self._users.pop(str(user_id), None) is not None
        if existed:
            self._save()
        return existed


# ----------------------------------------------------------------- OAuth ---

class SpotifyAuth:
    def __init__(self, store: UserStore):
        self.store = store
        self._pending: dict[int, dict] = {}            # user_id -> PKCE state
        self._tokens: dict[int, tuple[str, float]] = {}  # user_id -> (token, expiry)

    def login_url(self, user_id: int, client_id: str) -> str:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        self._pending[user_id] = {
            "client_id": client_id.strip(), "verifier": verifier, "state": state, "t": time.time(),
        }
        return AUTH_URL + "?" + urlencode({
            "client_id": client_id.strip(),
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "scope": SCOPES,
            "state": state,
        })

    async def finish_login(self, user_id: int, pasted: str) -> dict:
        pending = self._pending.get(user_id)
        if not pending or time.time() - pending["t"] > 900:
            raise SpotifyError("The login link expired. Run `/spotify login` again.")
        text = pasted.strip()
        query = parse_qs(urlparse(text).query) if "?" in text else {}
        if query.get("error"):
            raise SpotifyError(f"Spotify login was cancelled ({query['error'][0]}).")
        code = (query.get("code") or [text])[0]
        if query.get("state") and query["state"][0] != pending["state"]:
            raise SpotifyError("That address belongs to a different login attempt. Run `/spotify login` again.")
        if not code or " " in code or len(code) < 20:
            raise SpotifyError("That doesn't look like the address Spotify sent you to. Copy the whole address bar.")

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": pending["client_id"],
                "code_verifier": pending["verifier"],
            })
            if r.status_code != 200:
                raise SpotifyError(_token_error(r))
            tok = r.json()
            me = await client.get(f"{API}/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
            if me.status_code == 403 and "premium" in me.text.lower():
                raise PremiumRequired()
            if me.status_code != 200:
                raise SpotifyError(f"Logged in, but Spotify refused to share the account (HTTP {me.status_code}).")
            profile = me.json()

        data = {
            "client_id": pending["client_id"],
            "refresh_token": tok["refresh_token"],
            "username": profile.get("id", ""),
            "display_name": profile.get("display_name") or profile.get("id", ""),
        }
        self.store.put(user_id, data)
        self._tokens[user_id] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        self._pending.pop(user_id, None)
        return data

    async def access_token(self, user_id: int) -> str:
        cached = self._tokens.get(user_id)
        if cached and cached[1] > time.time():
            return cached[0]
        data = self.store.get(user_id)
        if not data:
            raise SpotifyError("You haven't connected Spotify yet. Run `/spotify login` first.")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "client_id": data["client_id"],
            })
        if r.status_code != 200:
            if r.status_code == 400 and "invalid_grant" in r.text:
                raise SpotifyError("Your Spotify login has expired or was removed. Run `/spotify login` again.")
            raise SpotifyError(_token_error(r))
        tok = r.json()
        if tok.get("refresh_token") and tok["refresh_token"] != data["refresh_token"]:
            data["refresh_token"] = tok["refresh_token"]
            self.store.put(user_id, data)
        self._tokens[user_id] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 60)
        return tok["access_token"]

    def forget(self, user_id: int) -> bool:
        self._tokens.pop(user_id, None)
        self._pending.pop(user_id, None)
        shutil.rmtree(SESSIONS_DIR / str(user_id), ignore_errors=True)
        return self.store.delete(user_id)

    async def api_get(self, user_id: int, path: str, params: Optional[dict] = None) -> dict:
        token = await self.access_token(user_id)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 403 and "premium" in r.text.lower():
            raise PremiumRequired()
        if r.status_code == 429:
            raise SpotifyError("Spotify is rate limiting this app. Try again in a minute.")
        if r.status_code != 200:
            raise SpotifyError(f"Spotify answered HTTP {r.status_code}.")
        return r.json()

    async def search(self, user_id: int, query: str, limit: int = 5) -> list[dict]:
        data = await self.api_get(user_id, "/search", {"q": query, "type": "track", "limit": min(limit, 10)})
        out = []
        for t in (data.get("tracks") or {}).get("items") or []:
            out.append(_track_info(t))
        return out

    async def track(self, user_id: int, track_id: str) -> dict:
        return _track_info(await self.api_get(user_id, f"/tracks/{track_id}"))


def _track_info(t: dict) -> dict:
    images = (t.get("album") or {}).get("images") or []
    return {
        "uri": t.get("uri"),
        "name": t.get("name") or "Unknown",
        "artists": ", ".join(a.get("name", "") for a in t.get("artists") or []),
        "album": (t.get("album") or {}).get("name", ""),
        "duration_ms": t.get("duration_ms") or 0,
        "image": images[0]["url"] if images else None,
    }


def _token_error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        body = {}
    err = body.get("error", "")
    desc = body.get("error_description", "")
    if err == "invalid_client":
        return "Spotify doesn't recognise that Client ID. Copy it again from your app's Settings page."
    if "redirect" in desc.lower():
        return f"Your Spotify app's Redirect URI must be exactly {REDIRECT_URI}"
    if err == "invalid_grant":
        return "That login code was already used or expired. Run `/spotify login` again."
    return f"Spotify login failed ({err or r.status_code}{': ' + desc if desc else ''})."


# --------------------------------------------------------------- player ----

class LibrespotSession:
    """One go-librespot process logged in as one user, writing PCM to a FIFO."""

    def __init__(self, user_id: int, guild_id: int, port: int):
        self.user_id = user_id
        self.guild_id = guild_id
        self.port = port
        self.dir = SESSIONS_DIR / str(user_id)
        self.fifo = self.dir / "audio.pipe"
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.base = f"http://127.0.0.1:{port}"
        self.last_activity = time.time()
        self.now_playing: dict = {}
        self.paused = False
        self._events_task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None
        self._log_tail: list[str] = []
        self.on_event = None  # callback(session, type, data)
        self.audio_refused = asyncio.Event()
        self.started_playing = asyncio.Event()

    def _write_config(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)
        cfg = {
            "device_name": "Discord Music Bot",
            "device_type": "speaker",
            "audio_backend": "pipe",
            "audio_output_pipe": str(self.fifo),
            "audio_output_pipe_format": "s16le",
            "audio_output_pipe_wait_for_reader": True,
            "bitrate": 160,
            "volume_steps": 100,
            "initial_volume": 100,
            "external_volume": True,   # Discord-side volume; keep samples untouched
            "zeroconf_enabled": False,
            # Don't keep "radio" playing on its own: when a song can't play
            # it would try dozens of related songs in a row.
            "disable_autoplay": True,
            "log_level": "info",
            "cache": {"enabled": False},
            # Spotify only streams audio to clients logged in through its own
            # login; tokens from a user's developer app fail with
            # INVALID_CREDENTIALS. Device pairing (spotify.com/pair) is done
            # once; go-librespot then keeps the login in state.json here.
            "credentials": {"type": "device_auth"},
            "server": {"enabled": True, "address": "127.0.0.1", "port": self.port},
        }
        # JSON is valid YAML, so no YAML library is needed.
        path = self.dir / "config.yml"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    @property
    def paired(self) -> bool:
        try:
            state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        creds = state.get("credentials") or {}
        return bool(creds.get("username") and creds.get("data"))

    async def start(self, binary: str, pairing: bool = False, on_pair_url=None, ready_timeout: float = 30):
        """Start the player. With pairing=True, report the spotify.com/pair
        link through on_pair_url and wait (up to ready_timeout) for approval."""
        self._write_config()
        if self.fifo.exists():
            self.fifo.unlink()
        os.mkfifo(self.fifo, 0o600)
        # Supervisor starts the bot without HOME, and go-librespot refuses to
        # run without HOME or XDG_CONFIG_HOME, so point both at this session.
        env = {**os.environ, "HOME": str(self.dir), "XDG_CONFIG_HOME": str(self.dir)}
        self.proc = await asyncio.create_subprocess_exec(
            binary, "--config_dir", str(self.dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env=env,
        )
        self._log_task = asyncio.create_task(self._read_logs())
        deadline = time.time() + ready_timeout
        reported = False
        async with httpx.AsyncClient(timeout=3) as client:
            while time.time() < deadline:
                if self.proc.returncode is not None:
                    break
                pair = next((m.group(1) for line in self._log_tail
                             if (m := re.search(r"visit (https://\S*spotify\.com/pair\S*)", line))), None)
                if pair:
                    if not pairing:
                        break  # no saved login: needs /spotify login
                    if not reported and on_pair_url:
                        reported = True
                        with contextlib.suppress(Exception):
                            await on_pair_url(pair)
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    r = await client.get(self.base + "/")
                    if r.status_code == 200 and r.json().get("playback_ready"):
                        self._events_task = asyncio.create_task(self._read_events())
                        return
                await asyncio.sleep(0.5)
        tail = "\n".join(self._log_tail[-15:]).lower()
        await self.stop()
        if "premium" in tail:
            raise PremiumRequired()
        if "spotify.com/pair" in tail and not pairing:
            raise SpotifyError("The Spotify player isn't linked to your account yet. Run `/spotify login` to link it.")
        if "invalid_credentials" in tail or "bad credentials" in tail:
            with contextlib.suppress(OSError):
                (self.dir / "state.json").unlink()
            raise SpotifyError("Spotify rejected the player's saved login. Run `/spotify login` to link it again.")
        if pairing and "spotify.com/pair" in tail:
            raise SpotifyError("The link wasn't approved in time. Run `/spotify login` to get a new link.")
        logger.error("go-librespot failed to start. Last output:\n%s", "\n".join(self._log_tail[-15:]))
        raise SpotifyError("The Spotify player could not start. Try again, or log in again with `/spotify login`.")

    async def _read_logs(self):
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            # Never keep anything token-like in memory logs.
            text = re.sub(r"(access_token|token)[\"'=: ]+\S+", r"\1=***", text, flags=re.I)
            self._log_tail.append(text)
            del self._log_tail[:-50]
            if "refused the audio key" in text or "failed retrieving aes key" in text:
                self.audio_refused.set()
            if "level=error" in text or "level=warn" in text:
                logger.info("go-librespot[%s]: %s", self.user_id, text[:300])

    async def _read_events(self):
        import websockets

        url = f"ws://127.0.0.1:{self.port}/events"
        while self.proc and self.proc.returncode is None:
            try:
                async with websockets.connect(url, max_size=2**20) as ws:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        etype, data = msg.get("type"), msg.get("data") or {}
                        if etype == "metadata":
                            self.now_playing = data
                        elif etype in ("paused", "stopped", "not_playing"):
                            self.paused = True
                        elif etype == "playing":
                            self.paused = False
                            self.last_activity = time.time()
                            self.started_playing.set()
                        if self.on_event:
                            with contextlib.suppress(Exception):
                                self.on_event(self, etype, data)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(2)

    async def call(self, path: str, body: Optional[dict] = None, method: str = "POST"):
        self.last_activity = time.time()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(method, self.base + path, json=body)
        if r.status_code >= 400:
            raise SpotifyError(f"The Spotify player refused that ({r.status_code}): {r.text[:120]}")
        return r.json() if r.content and r.headers.get("content-type", "").startswith("application/json") else None

    async def play(self, uri: str, wait: float = 10):
        """Start playing and confirm Spotify really delivers audio."""
        self.audio_refused.clear()
        self.started_playing.clear()
        await self.call("/player/play", {"uri": uri, "paused": False})
        self.paused = False
        refused = asyncio.create_task(self.audio_refused.wait())
        playing = asyncio.create_task(self.started_playing.wait())
        done, pending = await asyncio.wait({refused, playing}, timeout=wait,
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if refused in done or self.audio_refused.is_set():
            raise AudioRefused()

    async def stop(self):
        for task in (self._events_task, self._log_task):
            if task:
                task.cancel()
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.fifo.unlink()
        self.proc = None

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)


class SessionManager:
    """At most MAX_SESSIONS players; one per guild; started and stopped on demand."""

    def __init__(self, auth: SpotifyAuth):
        self.auth = auth
        self.by_guild: dict[int, LibrespotSession] = {}
        self._pairing_ports: set[int] = set()
        self._lock = asyncio.Lock()

    def get(self, guild_id: int) -> Optional[LibrespotSession]:
        s = self.by_guild.get(guild_id)
        return s if s and s.alive else None

    async def ensure(self, guild_id: int, user_id: int) -> LibrespotSession:
        async with self._lock:
            current = self.by_guild.get(guild_id)
            if current and current.alive and current.user_id == user_id:
                return current
            if current:
                await current.stop()
                self.by_guild.pop(guild_id, None)
            # One Spotify account can stream on one device: move it if it's playing elsewhere.
            for gid, s in list(self.by_guild.items()):
                if s.user_id == user_id:
                    await s.stop()
                    self.by_guild.pop(gid, None)
            live = [s for s in self.by_guild.values() if s.alive]
            if len(live) >= MAX_SESSIONS:
                raise SpotifyError(
                    f"Spotify is already playing in {len(live)} places on this bot, which is the limit. "
                    "Try again when one of them stops."
                )
            binary = find_librespot()
            if not binary:
                raise SpotifyError("Spotify playback isn't installed on this bot's server yet.")
            if not self.auth.store.get(user_id):
                raise SpotifyError("You haven't connected Spotify yet. Run `/spotify login` first.")
            session = LibrespotSession(user_id, guild_id, self._free_port())
            await session.start(binary)
            self.by_guild[guild_id] = session
            logger.info("Started Spotify player for guild %s on port %s", guild_id, session.port)
            return session

    def _free_port(self) -> int:
        used = {s.port for s in self.by_guild.values()} | self._pairing_ports
        return next(p for p in range(BASE_PORT, BASE_PORT + 50) if p not in used)

    def is_paired(self, user_id: int) -> bool:
        return LibrespotSession(user_id, 0, 0).paired

    async def pair(self, user_id: int, on_pair_url) -> None:
        """Link the player to the user's account once via spotify.com/pair."""
        binary = find_librespot()
        if not binary:
            raise SpotifyError("Spotify playback isn't installed on this bot's server yet.")
        await self.stop_user(user_id)
        port = self._free_port()
        self._pairing_ports.add(port)
        session = LibrespotSession(user_id, 0, port)
        try:
            await session.start(binary, pairing=True, on_pair_url=on_pair_url, ready_timeout=600)
        finally:
            self._pairing_ports.discard(port)
            await session.stop()
        if not session.paired:
            raise SpotifyError("Linking didn't finish. Run `/spotify login` to try again.")

    async def stop(self, guild_id: int):
        async with self._lock:
            s = self.by_guild.pop(guild_id, None)
        if s:
            await s.stop()
            logger.info("Stopped Spotify player for guild %s", guild_id)

    async def stop_user(self, user_id: int):
        for gid, s in list(self.by_guild.items()):
            if s.user_id == user_id:
                await self.stop(gid)

    async def stop_all(self):
        for gid in list(self.by_guild):
            await self.stop(gid)
