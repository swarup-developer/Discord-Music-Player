import time
import logging
import discord

logger = logging.getLogger(__name__)

class JitterBuffer(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, max_delay_ms: int = 1000):
        self.source = source
        self.original = source
        self.max_delay_frames = max_delay_ms // 20
        self.last_read_time = None
        self.delay_count = 0

    def read(self) -> bytes:
        while True:
            data = self.source.read()
            if not data:
                return b""
            
            now = time.perf_counter()
            if self.last_read_time is not None:
                elapsed = now - self.last_read_time
                if elapsed < 0.018:
                    self.delay_count += 1
                    if self.delay_count > self.max_delay_frames:
                        # Drop this frame to catch up and resync
                        continue
                    else:
                        time.sleep(0.02 - elapsed)
                else:
                    self.delay_count = max(0, self.delay_count - 1)
            self.last_read_time = time.perf_counter()
            return data

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()
