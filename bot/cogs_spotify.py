"""Spotify (Premium) playback, straight from Spotify.

/spotify login shows a Spotify pairing code: open the link (phone or PC),
press Agree, done. The login is stored on the bot's server only.
Then choose Spotify in /provider and /play searches and plays from Spotify;
the normal pause, resume, skip, previous, seek, stop and volume controls work.

Audio comes from dcbot-spotify (spotify-helper/, built on librespot): one
helper process per playing server, raw PCM piped into FFmpeg for Discord.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = ROOT / "spotify_sessions"
IDLE_SECONDS = 300
LINK_RE = re.compile(r"(open\.spotify\.com/|^spotify:)")


def find_helper() -> Optional[str]:
    for p in (os.environ.get("DCBOT_SPOTIFY_BIN"), ROOT / "bin" / "dcbot-spotify", shutil.which("dcbot-spotify")):
        if p and Path(p).is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def user_dir(user_id: int) -> Path:
    return SESSIONS / str(int(user_id))


def is_link(text: str) -> bool:
    return bool(LINK_RE.search(text.strip()))


def describe(t: dict) -> str:
    artists = ", ".join(t.get("artists") or [])
    return f"{t.get('name', 'Unknown')} - {artists}" if artists else t.get("name", "Unknown")


class SpotifyError(Exception):
    pass


# --------------------------------------------------------------- player ---

class GuildPlayer:
    """One dcbot-spotify process playing for one server."""

    def __init__(self, cog: "SpotifyCog", guild_id: int, user_id: int):
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.ready = asyncio.Event()
        self.account: dict = {}
        self.error = ""
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future] = {}
        self.queue: list[dict] = []
        self.history: list[dict] = []
        self.current: Optional[dict] = None
        self.paused = False
        self.position_ms = 0
        self.position_at = time.time()
        self.last_activity = time.time()
        self.key_refused = False
        self.source: Optional[discord.AudioSource] = None
        self.pcm = None
        self.text_channel_id: Optional[int] = None
        self._tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, binary: str):
        # PCM goes through a plain OS pipe: FFmpeg (via discord.py) reads it
        # with a normal blocking read.
        r_fd, w_fd = os.pipe()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                binary, "serve", str(user_dir(self.user_id)),
                stdin=asyncio.subprocess.PIPE, stdout=w_fd, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "RUST_LOG": "warn"}, limit=1 << 20,
            )
        finally:
            os.close(w_fd)
        self.pcm = os.fdopen(r_fd, "rb", buffering=0)
        self._tasks.append(asyncio.create_task(self._read_events()))
        try:
            await asyncio.wait_for(self.ready.wait(), 30)
        except asyncio.TimeoutError:
            await self.stop()
            raise SpotifyError(self.error or "Spotify didn't answer. Try again in a moment.")
        if not self.alive:
            raise SpotifyError(self.error or "Spotify sign-in failed. Run `/spotify login` again.")

    async def _read_events(self):
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                low = text.lower()
                if "audio key" in low:
                    self.key_refused = True
                if text:
                    logger.info("spotify[%s]: %s", self.guild_id, text[:300])
                continue
            try:
                ev = json.loads(text)
            except ValueError:
                continue
            with contextlib.suppress(Exception):
                await self._handle(ev)
        self.error = self.error or "The Spotify player stopped."
        self.ready.set()
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(SpotifyError(self.error))
        self.cog._player_gone(self)

    async def _handle(self, ev: dict):
        kind = ev.get("ev")
        if kind == "ready":
            self.account = ev
            self.ready.set()
        elif kind == "error" and "id" not in ev:
            self.error = ev.get("message", "")
            logger.warning("spotify[%s] error: %s", self.guild_id, self.error)
        elif kind == "result":
            fut = self._waiting.pop(ev.get("id"), None)
            if fut and not fut.done():
                fut.set_result(ev)
        elif kind in ("playing", "paused", "pos"):
            self.position_ms, self.position_at = int(ev.get("pos") or 0), time.time()
            if kind != "pos":
                self.paused = kind == "paused"
                self.last_activity = time.time()
                self.key_refused = False if kind == "playing" else self.key_refused
                self.cog._sync_controller(self)
        elif kind == "preload_next" and self.queue:
            nxt = self.queue[0]
            if nxt.get("uri"):
                await self.send("preload", uri=nxt["uri"])
        elif kind == "end":
            await self.next(auto=True)
        elif kind == "unavailable":
            name = describe(self.current) if self.current else "This song"
            if self.key_refused:
                msg = (f"Spotify wouldn't send the audio for **{name}** to this account, so it was skipped. "
                       "This is a block on Spotify's side for this account.")
            else:
                msg = f"**{name}** isn't available on Spotify here, skipping."
            await self.cog._say(self, msg)
            await self.next(auto=True)

    async def send(self, cmd: str, **fields):
        if not self.alive or not self.proc or not self.proc.stdin:
            raise SpotifyError("The Spotify player isn't running.")
        self.proc.stdin.write((json.dumps({"cmd": cmd, **fields}) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, cmd: str, timeout: float = 25, **fields) -> dict:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        await self.send(cmd, id=rid, **fields)
        try:
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self._waiting.pop(rid, None)
        if res.get("error"):
            raise SpotifyError(f"Spotify: {res['error']}")
        return res

    def position(self) -> int:
        if self.paused or not self.current:
            return self.position_ms
        return self.position_ms + int((time.time() - self.position_at) * 1000)

    async def _fill(self, track: dict) -> dict:
        if track.get("name"):
            return track
        with contextlib.suppress(Exception):
            res = await self.request("meta", uris=[track["uri"]])
            if res.get("tracks"):
                return res["tracks"][0]
        return track

    async def play_track(self, track: dict):
        track = await self._fill(track)
        self.current = track
        self.paused = False
        self.position_ms, self.position_at = 0, time.time()
        self.last_activity = time.time()
        await self.send("load", uri=track["uri"], pos=0, play=True)
        self.cog._sync_controller(self, new_track=True)

    async def next(self, auto: bool = False):
        if self.current:
            self.history.append(self.current)
            del self.history[:-50]
        if not self.queue:
            self.current = None
            await self.send("stop")
            self.cog._sync_controller(self)
            if auto:
                await self.cog._say(self, "The Spotify queue is finished.")
            return False
        await self.play_track(self.queue.pop(0))
        return True

    async def prev(self):
        if not self.history:
            await self.send("seek", pos=0)
            return False
        if self.current:
            self.queue.insert(0, self.current)
        self.current = None
        await self.play_track(self.history.pop())
        return True

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        if self.alive and self.proc:
            with contextlib.suppress(Exception):
                await self.send("quit")
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
        if self.pcm:
            with contextlib.suppress(OSError):
                self.pcm.close()


# ------------------------------------------------------------------ cog ---

class SpotifyCog(commands.Cog):
    spotify = app_commands.Group(name="spotify", description="Play from your Spotify Premium account")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: dict[int, GuildPlayer] = {}
        self._pairing: set[int] = set()
        self._idle_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        SESSIONS.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(SESSIONS, 0o700)
        self._idle_task = asyncio.create_task(self._idle_loop())

    async def cog_unload(self):
        if self._idle_task:
            self._idle_task.cancel()
        for p in list(self.sessions.values()):
            await p.stop()

    # ---------------------------------------------------------- helpers ---

    @property
    def music(self):
        return self.bot.get_cog("MusicCog")

    def logged_in(self, user_id: int) -> bool:
        return (user_dir(user_id) / "auth" / "credentials.json").is_file()

    def _voice_client(self, guild_id: int) -> Optional[discord.VoiceClient]:
        music = self.music
        vc = music._session(guild_id).voice_client if music else None
        return vc if vc and vc.is_connected() else None

    def is_active(self, guild_id: int) -> bool:
        p = self.sessions.get(guild_id)
        vc = self._voice_client(guild_id)
        return bool(p and p.alive and p.current and vc and p.source is not None and vc.source is p.source)

    async def _say(self, player: GuildPlayer, text: str):
        ch = self.bot.get_channel(player.text_channel_id) if player.text_channel_id else None
        if ch:
            with contextlib.suppress(Exception):
                await ch.send(text)

    def _player_gone(self, player: GuildPlayer):
        if self.sessions.get(player.guild_id) is player:
            self.sessions.pop(player.guild_id, None)
            vc = self._voice_client(player.guild_id)
            if vc and player.source is not None and vc.source is player.source:
                vc.stop()
            self._clear_now_playing(player.guild_id)

    def _clear_now_playing(self, guild_id: int):
        music = self.music
        if not music:
            return
        s = music._session(guild_id)
        if getattr(s, "spotify_song", None) is not None:
            s.spotify_song = None
            s.current_song_title = None
            s.is_playing = False
            asyncio.create_task(music.refresh_controller(guild_id))

    def _sync_controller(self, player: GuildPlayer, new_track: bool = False):
        music = self.music
        if not music:
            return
        s = music._session(player.guild_id)
        t = player.current
        if not t:
            return self._clear_now_playing(player.guild_id)
        s.current_song_title = describe(t)
        s.current_song_data = None
        s.spotify_song = {"duration": (int(t.get("duration_ms") or 0) / 1000) or None, "image": t.get("image")}
        s.is_playing = not player.paused
        s.start_time = time.time() - player.position() / 1000
        s.paused_duration = 0.0
        s.last_paused_at = time.time() if player.paused else None
        vc = self._voice_client(player.guild_id)
        if vc and player.source is not None and vc.source is player.source:
            if player.paused and vc.is_playing():
                vc.pause()
            elif not player.paused and vc.is_paused():
                vc.resume()
        asyncio.create_task(music.refresh_controller(player.guild_id))

    def _attach_audio(self, player: GuildPlayer, vc: discord.VoiceClient):
        if player.source is not None and vc.source is player.source:
            if vc.is_paused():
                vc.resume()
            return
        music = self.music
        if music:
            music._stop_current_track_for_manual_replace(player.guild_id)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        volume = music.state.get_volume(player.guild_id) if music else 0.5
        pcm = discord.FFmpegPCMAudio(
            player.pcm, pipe=True,
            before_options="-f s16le -ar 44100 -ac 2 -thread_queue_size 1024", options="-vn",
        )
        source = discord.PCMVolumeTransformer(pcm, volume=volume ** 2)
        player.source = source

        def after(error, p=player, src=source):
            if error:
                logger.warning("Spotify audio ended with error: %s", error)
            if p.source is src:
                p.source = None
                if self.sessions.get(p.guild_id) is p:
                    asyncio.run_coroutine_threadsafe(self._detached(p), self.bot.loop)

        vc.play(source, after=after)

    async def _detached(self, player: GuildPlayer):
        """Another source took over the voice channel (or it disconnected)."""
        with contextlib.suppress(Exception):
            await player.send("pause")
        player.paused = True
        self._clear_now_playing(player.guild_id)

    async def _player_for(self, guild_id: int, user_id: int) -> GuildPlayer:
        p = self.sessions.get(guild_id)
        if p and p.alive and p.user_id == user_id:
            return p
        if p:
            await self._drop(guild_id)
        # One Spotify account streams in one place at a time.
        for gid, other in list(self.sessions.items()):
            if other.user_id == user_id:
                await self._drop(gid)
                await self._say(other, "Spotify moved to another server with this account.")
        binary = find_helper()
        if not binary:
            raise SpotifyError("Spotify playback isn't installed on this bot's server yet.")
        p = GuildPlayer(self, guild_id, user_id)
        self.sessions[guild_id] = p
        try:
            await p.start(binary)
        except Exception:
            self.sessions.pop(guild_id, None)
            raise
        return p

    async def _drop(self, guild_id: int):
        p = self.sessions.pop(guild_id, None)
        if not p:
            return
        vc = self._voice_client(guild_id)
        if vc and p.source is not None and vc.source is p.source:
            p.source = None
            vc.stop()
        await p.stop()
        self._clear_now_playing(guild_id)

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for gid, p in list(self.sessions.items()):
                if not p.alive:
                    self.sessions.pop(gid, None)
                elif not self._voice_client(gid) and now - p.last_activity > 60:
                    await self._drop(gid)
                elif (p.paused or not p.current) and now - p.last_activity > IDLE_SECONDS:
                    await self._drop(gid)

    # ------------------------------------------------ called by MusicCog ---

    async def play(self, interaction: discord.Interaction, query: str):
        """Returns False when Spotify shouldn't handle this (a link from someone
        who isn't logged in): MusicCog then plays it the usual way."""
        guild_id = interaction.guild.id
        music = self.music
        provider = music.state.get_provider(guild_id) if music else ""
        current = self.sessions.get(guild_id)
        account = current.user_id if current and current.alive else interaction.user.id
        if not self.logged_in(account):
            if provider != "spotify":
                return False
            return await interaction.followup.send(
                "This server plays from **Spotify**, and you aren't connected yet. Run `/spotify login` "
                "to connect your Spotify Premium account, or pick another source with `/provider`."
            )
        vc = self._voice_client(guild_id)
        if not vc:
            return await interaction.followup.send("Use `/join` first so the bot is in your voice channel.")
        try:
            player = await self._player_for(guild_id, account)
            player.text_channel_id = interaction.channel_id
            found = await player.request("resolve", q=query)
            tracks = list(found.get("tracks") or []) + [{"uri": u} for u in found.get("rest") or []]
            if not tracks:
                return await interaction.followup.send(f"Nothing found on Spotify for **{query}**.")
            busy = self.is_active(guild_id) and player.current is not None
            if busy:
                player.queue.extend(tracks)
                first = tracks[0]
                more = f" and {len(tracks) - 1} more" if len(tracks) > 1 else ""
                return await interaction.followup.send(f"Queued **{describe(first)}**{more} from Spotify.")
            self._attach_audio(player, vc)
            player.queue[:0] = tracks[1:]
            await player.play_track(tracks[0])
        except SpotifyError as e:
            return await interaction.followup.send(str(e))
        except asyncio.TimeoutError:
            return await interaction.followup.send("Spotify is slow to answer right now. Try again.")
        except Exception:
            logger.exception("Spotify play failed")
            return await interaction.followup.send("Spotify couldn't play that. Try again.")
        kind = found.get("kind")
        if kind in ("album", "playlist"):
            return await interaction.followup.send(
                f"Playing the Spotify {kind} **{found.get('title')}** ({len(tracks)} songs).")
        await interaction.followup.send(f"Playing from Spotify: **{describe(player.current or tracks[0])}**")

    async def control(self, guild_id: int, action: str, seconds: float = 0) -> str:
        p = self.sessions.get(guild_id)
        if not p or not p.alive:
            return "Spotify isn't playing."
        p.last_activity = time.time()
        vc = self._voice_client(guild_id)
        try:
            if action == "pause":
                await p.send("pause")
                p.paused = True
                if vc and vc.is_playing():
                    vc.pause()
                self._sync_controller(p)
                return "Paused."
            if action == "resume":
                if vc and p.source is None:
                    self._attach_audio(p, vc)
                await p.send("play")
                p.paused = False
                if vc and vc.is_paused():
                    vc.resume()
                self._sync_controller(p)
                return "Resumed."
            if action == "next":
                return "Skipped to the next song." if await p.next() else "That was the last song in the queue."
            if action == "prev":
                return "Playing the previous song." if await p.prev() else "Back to the start of the song."
            if action == "seek":
                pos = max(0, p.position() + int(seconds * 1000))
                await p.send("seek", pos=pos)
                p.position_ms, p.position_at = pos, time.time()
                self._sync_controller(p)
                return f"Moved {'forward' if seconds > 0 else 'back'} {abs(int(seconds))} seconds."
            if action == "stop":
                p.queue.clear()
                await self._drop(guild_id)
                return "Stopped."
        except SpotifyError as e:
            return str(e)
        return "Unknown action."

    # ----------------------------------------------------------- commands ---

    @spotify.command(name="login", description="Connect your Spotify Premium account with a pairing code")
    async def login_cmd(self, interaction: discord.Interaction):
        uid = interaction.user.id
        binary = find_helper()
        if not binary:
            return await interaction.response.send_message(
                "Spotify playback isn't installed on this bot's server yet. Ask the bot owner.", ephemeral=True)
        if self.logged_in(uid):
            return await interaction.response.send_message(
                "You're already connected. Choose **Spotify** in `/provider` and use `/play`. "
                "To use a different account, run `/spotify logout` first.", ephemeral=True)
        if uid in self._pairing:
            return await interaction.response.send_message(
                "Your Spotify pairing is already waiting. Use the link I sent you.", ephemeral=True)
        await interaction.response.send_message("Getting a pairing code from Spotify, one moment.", ephemeral=True)
        self._pairing.add(uid)
        tmp = SESSIONS / f".pair-{uid}"
        shutil.rmtree(tmp, ignore_errors=True)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "pair", str(tmp), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "RUST_LOG": "warn"},
            )
            result, error = None, ""
            deadline = time.time() + 900
            while time.time() < deadline:
                line = await asyncio.wait_for(proc.stderr.readline(), deadline - time.time())
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                ev = json.loads(text)
                if ev.get("ev") == "code":
                    view = discord.ui.View(timeout=900)
                    view.add_item(discord.ui.Button(label="Open Spotify pairing", url=ev["url"]))
                    await interaction.followup.send(
                        "Press **Open Spotify pairing** (on your phone or PC), log in with your Spotify Premium "
                        f"account and press **Agree**. If Spotify asks for a code, it's **{ev['code']}**.\n"
                        "I'll finish by myself and tell you here.", view=view, ephemeral=True)
                elif ev.get("ev") == "paired":
                    result = ev
                    break
                elif ev.get("ev") == "error":
                    error = ev.get("message", "")
            await asyncio.wait_for(proc.wait(), 10)
            if not result:
                logger.warning("Spotify pairing failed: %s", error)
                msg = "The pairing code expired." if "expired" in error.lower() else "Spotify login didn't finish."
                return await interaction.followup.send(f"{msg} Run `/spotify login` again.", ephemeral=True)
            if result.get("product") and result["product"] != "premium":
                return await interaction.followup.send(
                    "That Spotify account isn't Premium. Spotify only allows playback in other apps "
                    "with Spotify Premium.", ephemeral=True)
            dest = user_dir(uid)
            shutil.rmtree(dest, ignore_errors=True)
            tmp.rename(dest)
            with contextlib.suppress(OSError):
                os.chmod(dest, 0o700)
        except asyncio.TimeoutError:
            return await interaction.followup.send("The pairing code expired. Run `/spotify login` again.", ephemeral=True)
        except Exception:
            logger.exception("Spotify pairing failed")
            return await interaction.followup.send("Spotify login failed. Run `/spotify login` again.", ephemeral=True)
        finally:
            self._pairing.discard(uid)
            if proc and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)
        await interaction.followup.send(
            "Spotify is connected. Choose **Spotify** in `/provider`, then use `/play` as usual.", ephemeral=True)

    @spotify.command(name="logout", description="Remove your Spotify login from this bot")
    async def logout_cmd(self, interaction: discord.Interaction):
        uid = interaction.user.id
        for gid, p in list(self.sessions.items()):
            if p.user_id == uid:
                await self._drop(gid)
        existed = user_dir(uid).exists()
        shutil.rmtree(user_dir(uid), ignore_errors=True)
        await interaction.response.send_message(
            "Your Spotify login is removed from this bot." if existed else "You weren't connected.", ephemeral=True)
