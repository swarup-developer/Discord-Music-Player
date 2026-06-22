import time
import logging
import discord

logger = logging.getLogger(__name__)

class JitterBuffer(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, max_delay_ms: int = 1000):
        self.source = source
        self.original = source
        self.last_read_time = None

    def read(self) -> bytes:
        data = self.source.read()
        if not data:
            return b""
        
        now = time.perf_counter()
        if self.last_read_time is not None:
            elapsed = now - self.last_read_time
            # Enforce 20ms pacing to prevent 2.5x catch-up from FFmpeg bursts
            if elapsed < 0.019:
                time.sleep(0.02 - elapsed)
        self.last_read_time = time.perf_counter()
        return data

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()
