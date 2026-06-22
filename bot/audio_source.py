import time
import logging
import discord

logger = logging.getLogger(__name__)

class JitterBuffer(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, max_delay_ms: int = 1000):
        self.source = source
        self.original = source
        self.expected_time = None

    def read(self) -> bytes:
        data = self.source.read()
        if not data:
            return b""
        
        now = time.perf_counter()
        if self.expected_time is None:
            self.expected_time = now
        
        self.expected_time += 0.02
        delay = self.expected_time - now
        
        # Limit the maximum drift (lag spike recovery)
        if delay < -0.1:
            self.expected_time = now
        elif delay > 0:
            time.sleep(delay)
            
        return data

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()
