import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Optional

import discord


@dataclass
class QueueItem:
    query: str
    title: str = "Unknown"
    requested_by: Optional[int] = None


@dataclass
class GuildSession:
    voice_client: Optional[discord.VoiceClient] = None
    voice_channel_id: Optional[int] = None
    linked_text_channel_id: Optional[int] = None
    current_song_title: Optional[str] = None
    current_guild_id: Optional[int] = None
    is_playing: bool = False
    temp_file_path: Optional[str] = None
    advance_queue_on_stop: bool = True
    voice_mode_enabled: bool = False
    voice_assistant_task: Optional[object] = None
    voice_assistant_source: Optional[object] = None
    current_song_data: Optional[dict] = None
    history: list[dict] = field(default_factory=list)


class BotState:
    def __init__(self):
        self.sessions: dict[int, GuildSession] = {}
        self.volume_by_guild: dict[int, float] = {}
        self.queues: dict[int, Deque[QueueItem]] = defaultdict(deque)
        self.controller_message_id: dict[int, int] = {}
        self.repeat_enabled: dict[int, bool] = {}
        self.shuffle_enabled: dict[int, bool] = {}
        self.loop_modes: dict[int, str] = {}
        self.effects: dict[int, list[str]] = {}
        self.providers: dict[int, str] = {}

    def is_active_in_other_guild(self, guild_id: int) -> bool:
        for session_guild_id, session in self.sessions.items():
            if session_guild_id != guild_id and session.voice_client and session.voice_client.is_connected():
                return True
        return False

    def session_for(self, guild_id: int) -> GuildSession:
        session = self.sessions.get(guild_id)
        if session is None:
            session = GuildSession()
            self.sessions[guild_id] = session
        return session

    def set_active(self, voice_client: discord.VoiceClient, voice_channel_id: int, text_channel_id: int, guild_id: int):
        session = self.session_for(guild_id)
        session.voice_client = voice_client
        session.voice_channel_id = voice_channel_id
        session.linked_text_channel_id = text_channel_id
        session.current_guild_id = guild_id

    def clear(self, guild_id: Optional[int] = None):
        if guild_id is None:
            return
        session = self.sessions.get(guild_id)
        if not session:
            return
        try:
            if session.temp_file_path and os.path.exists(session.temp_file_path):
                os.remove(session.temp_file_path)
        except Exception:
            pass
        session.voice_client = None
        session.voice_channel_id = None
        session.linked_text_channel_id = None
        session.current_song_title = None
        session.current_guild_id = None
        session.is_playing = False
        session.temp_file_path = None
        session.advance_queue_on_stop = True
        session.current_song_data = None
        session.history.clear()
        self.loop_modes.pop(guild_id, None)
        self.effects.pop(guild_id, None)
        self.providers.pop(guild_id, None)

    def get_volume(self, guild_id: int) -> float:
        return self.volume_by_guild.get(guild_id, 0.45)

    def set_volume(self, guild_id: int, volume: float) -> None:
        self.volume_by_guild[guild_id] = volume

    def queue_for(self, guild_id: int) -> Deque[QueueItem]:
        return self.queues[guild_id]

    def clear_queue(self, guild_id: int) -> None:
        self.queues.pop(guild_id, None)

    def get_loop_mode(self, guild_id: int) -> str:
        return self.loop_modes.get(guild_id, "off")

    def set_loop_mode(self, guild_id: int, mode: str) -> None:
        self.loop_modes[guild_id] = mode

    def get_effects(self, guild_id: int) -> list[str]:
        return self.effects.get(guild_id, [])

    def toggle_effect(self, guild_id: int, effect: str) -> bool:
        current = self.get_effects(guild_id)
        if effect in current:
            current = [e for e in current if e != effect]
            self.effects[guild_id] = current
            return False
        else:
            current = list(current) + [effect]
            self.effects[guild_id] = current
            return True

    def get_provider(self, guild_id: int) -> str:
        return self.providers.get(guild_id, "youtube")

    def set_provider(self, guild_id: int, provider: str) -> None:
        self.providers[guild_id] = provider
