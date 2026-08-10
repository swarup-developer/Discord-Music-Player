import logging

import discord

logger = logging.getLogger(__name__)


class JitterBuffer(discord.AudioSource):
    """Transparent pass-through audio wrapper.

    discord.py's ``AudioPlayer`` already paces playback to real time: it reads
    exactly one 20ms frame per iteration and sleeps until the next 20ms
    boundary (``_do_run`` in ``discord.player``). Adding a sleep inside
    ``read()`` throttled every frame to ~20ms **on top of** that pacing, which
    made the send rate drop below real time and caused the audio to lag and
    stutter as the client's buffer drained.

    The wrapper is kept so the ``original`` attribute chain used by the cog to
    reach provider-specific attributes (e.g. ``temp_file_path``) keeps working.
    """

    def __init__(self, source: discord.AudioSource, max_delay_ms: int = 1000):
        self.source = source
        self.original = source

    def read(self) -> bytes:
        return self.source.read()

    def is_opus(self) -> bool:
        return self.source.is_opus()

    def cleanup(self) -> None:
        self.source.cleanup()
