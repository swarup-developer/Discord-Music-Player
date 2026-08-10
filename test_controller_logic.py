"""Targeted logic tests for the Next/Prev/queue/generation fixes (no network)."""
import asyncio
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, ".")

import discord

from bot.cogs_music import MusicCog
from bot.state import BotState, QueueItem


class FakeVoiceClient:
    def __init__(self):
        self.connected = True
        self.playing = True
        self.paused = False
        self.stopped = False
        self.after = None
        self.source = None

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def play(self, source, after=None):
        self.source = source
        self.after = after
        self.playing = True

    def stop(self):
        self.playing = False
        self.stopped = True
        if self.after:
            cb = self.after
            self.after = None
            cb(None)

    def pause(self):
        self.paused = True
        self.playing = False

    def resume(self):
        self.paused = False
        self.playing = True


class FakeBot:
    def __init__(self):
        self.music_state = BotState()
        self.loop = asyncio.new_event_loop()

    def get_guild(self, guild_id):
        return types.SimpleNamespace(
            id=guild_id,
            get_channel=lambda cid: types.SimpleNamespace(send=mock.AsyncMock()),
        )


class FakeInteraction:
    def __init__(self, guild_id):
        self.guild = types.SimpleNamespace(id=guild_id)
        self.user = types.SimpleNamespace(id=123)
        self.followup = mock.AsyncMock()
        self.response = types.SimpleNamespace(is_done=lambda: False, defer=mock.AsyncMock())


def make_cog():
    bot = FakeBot()
    cog = MusicCog.__new__(MusicCog)
    cog.bot = bot
    cog.state = bot.music_state
    cog.voice_lock = asyncio.Lock()
    cog.empty_voice_tasks = {}
    cog.voice_join_cooldowns = {}
    return bot, cog


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestControllerLogic(unittest.TestCase):
    def setUp(self):
        self.bot, self.cog = make_cog()
        self.guild_id = 111
        self.vc = FakeVoiceClient()
        self.session = self.cog._session(self.guild_id)
        self.session.voice_client = self.vc
        self.session.linked_text_channel_id = 222

    def test_next_button_sets_skip_and_stops(self):
        # Build the controller view (button handlers need a cog + guild_id)
        view = MusicCog.MusicControllerView(self.cog, self.guild_id)
        next_btn = next(i for i in view.children if getattr(i, "label", None) == "Next")
        interaction = FakeInteraction(self.guild_id)

        self.cog.refresh_controller = mock.AsyncMock()
        # In discord.py 2.7 the button callback is pre-bound to the button,
        # so it only takes the interaction.
        run(next_btn.callback(interaction))
        self.assertTrue(self.session.advance_queue_on_stop)
        self.assertTrue(self.session.skip_requested)
        self.assertTrue(self.vc.stopped)

    def test_play_next_in_queue_uses_song_data(self):
        song = {"id": "some-jiosaavn-pid", "song": "Kesariya", "provider": "jiosaavn"}
        self.cog.state.queue_for(self.guild_id).append(
            QueueItem(query="some-jiosaavn-pid", title="Kesariya", song_data=song)
        )
        self.cog._play_song_data = mock.AsyncMock()
        self.cog._play_audio = mock.AsyncMock()

        run(self.cog._play_next_in_queue(self.guild_id, last_song_data=None))
        self.cog._play_song_data.assert_awaited_once_with(song, mock.ANY)
        self.cog._play_audio.assert_not_awaited()

    def test_play_next_in_queue_repeat_one_honors_skip(self):
        song_a = {"id": "pid-a", "song": "Song A", "provider": "jiosaavn"}
        song_b = {"id": "pid-b", "song": "Song B", "provider": "jiosaavn"}
        self.cog.state.set_loop_mode(self.guild_id, "song")
        self.cog.state.queue_for(self.guild_id).append(
            QueueItem(query="pid-b", title="Song B", song_data=song_b)
        )
        self.session.skip_requested = True
        self.cog._play_song_data = mock.AsyncMock()
        self.cog._play_audio = mock.AsyncMock()

        run(self.cog._play_next_in_queue(self.guild_id, last_song_data={"type": "song", "song": song_a, "title": "Song A"}))
        # With skip, the NEXT queue item (B) plays, not the repeated A.
        self.cog._play_song_data.assert_awaited_once_with(song_b, mock.ANY)
        self.assertFalse(self.session.skip_requested)

    def test_stale_after_callback_cannot_clobber_new_track(self):
        # Simulate: track A playing, user hits Prev -> new track B starts
        # (bumps generation), then A's after callback fires late.
        song_a = {"id": "pid-a", "song": "Song A", "provider": "jiosaavn"}
        song_b = {"id": "pid-b", "song": "Song B", "provider": "jiosaavn"}

        # Track A starts (generation 1)
        self.session.is_playing = True
        self.session.current_song_title = "Song A"
        self.session.current_song_data = {"type": "song", "song": song_a, "title": "Song A"}
        self.session.playback_generation += 1
        gen_a = self.session.playback_generation
        after_a = self.cog._make_after_playback(self.guild_id, self.session, gen_a, None)
        self.vc.play(mock.Mock(), after=after_a)

        # Track B starts (generation 2) - replaces A
        self.session.is_playing = True
        self.session.current_song_title = "Song B"
        self.session.current_song_data = {"type": "song", "song": song_b, "title": "Song B"}
        self.session.playback_generation += 1
        after_b = self.cog._make_after_playback(self.guild_id, self.session, self.session.playback_generation, None)
        self.vc.play(mock.Mock(), after=after_b)

        # A's after callback fires late (stale)
        after_a(None)

        # B's state must be untouched
        self.assertTrue(self.session.is_playing)
        self.assertEqual(self.session.current_song_title, "Song B")
        self.assertEqual(self.session.current_song_data["title"], "Song B")
        # A must not be added to history by the stale callback
        self.assertNotIn({"type": "song", "song": song_a, "title": "Song A"}, self.session.history)

    def test_current_after_callback_appends_history_and_advances(self):
        self.cog.refresh_controller = mock.AsyncMock()
        self.cog._play_next_in_queue = mock.AsyncMock()

        song_a = {"id": "pid-a", "song": "Song A", "provider": "jiosaavn"}
        self.session.is_playing = True
        self.session.current_song_title = "Song A"
        self.session.current_song_data = {"type": "song", "song": song_a, "title": "Song A"}
        self.session.playback_generation += 1
        after = self.cog._make_after_playback(self.guild_id, self.session, self.session.playback_generation, None)
        self.vc.play(mock.Mock(), after=after)

        # Natural end: advance flag is on, callback should append history and advance
        self.vc.stop()
        # Let the coroutine scheduled via run_coroutine_threadsafe execute.
        self.bot.loop.run_until_complete(asyncio.sleep(0))

        self.assertEqual(len(self.session.history), 1)
        self.assertEqual(self.session.history[0]["title"], "Song A")
        self.cog._play_next_in_queue.assert_awaited_once()

    def test_play_previous_preserves_song_data(self):
        song_a = {"id": "pid-a", "song": "Song A", "provider": "jiosaavn"}
        self.session.history.append({"type": "song", "song": song_a, "title": "Song A"})
        song_b = {"id": "pid-b", "song": "Song B", "provider": "jiosaavn"}
        self.session.current_song_data = {"type": "song", "song": song_b, "title": "Song B"}
        self.cog._play_song_data = mock.AsyncMock()
        self.cog._play_audio = mock.AsyncMock()

        interaction = FakeInteraction(self.guild_id)
        run(self.cog.play_previous(self.guild_id, interaction))
        # Previous track A is played
        self.cog._play_song_data.assert_awaited_once_with(song_a, interaction)
        # Current track B was pushed to the FRONT of the queue with song_data
        queue = self.cog.state.queue_for(self.guild_id)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].song_data, song_b)

    def test_spotify_queued_single_track_keeps_song_data(self):
        # Replicates _resolve_and_play's single-track queued branch
        song = {"id": "yt-id", "url": "https://www.youtube.com/watch?v=abc", "provider": "youtube", "title": "T"}
        queue = self.cog.state.queue_for(self.guild_id)
        queue.append(QueueItem(query=song.get("url") or song.get("id"), title="T", requested_by=123, song_data=song))
        self.assertEqual(queue[0].song_data, song)


if __name__ == "__main__":
    unittest.main(verbosity=2)
