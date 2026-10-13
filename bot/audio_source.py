import logging
import re

import discord

logger = logging.getLogger(__name__)


# Single source of truth for the equalizer presets. These filter strings were
# previously duplicated in youtube.py, jiosaavn.py, soundcloud.py and
# cogs_music.py; five copies meant a preset could silently disagree with itself
# between providers.
EQ_FILTERS = {
    "bassboost": "equalizer=f=60:width_type=o:width=2:g=8",
    "vocal": "equalizer=f=1000:width_type=q:width=1:g=5,equalizer=f=3000:width_type=q:width=1:g=3",
    "treble": "equalizer=f=8000:width_type=o:width=2:g=6",
    "lofi": "highpass=f=300,lowpass=f=4000",
    "acoustic": "equalizer=f=120:width_type=o:width=2:g=3,equalizer=f=2000:width_type=o:width=2:g=2,equalizer=f=8000:width_type=o:width=2:g=3",
}

_SS_PREFIX = re.compile(r"^\s*-ss\s+\S+\s*")


def build_ffmpeg_options(effects=None, eq_preset=None) -> str:
    """Build FFmpeg output options for the given effects and EQ preset.

    Any preset that is not a known key -- including the default "off" and
    None -- adds no equalizer filter at all, so untouched playback is
    unprocessed apart from the resample Discord requires.
    """
    filters = []
    preset = (eq_preset or "off").lower()
    if preset in EQ_FILTERS:
        filters.append(EQ_FILTERS[preset])

    if effects:
        if "bassboost" in effects and preset != "bassboost":
            filters.append(EQ_FILTERS["bassboost"])
        if "nightcore" in effects:
            filters.append("asetrate=48000*1.25")

    filters.append("aresample=osr=48000:osf=s16")
    return '-vn -flags low_delay -af "%s"' % ",".join(filters)


def apply_seek(before_options: str, seek=None) -> str:
    """Return ``before_options`` with any existing ``-ss`` replaced by ``seek``.

    Stripping first makes the function idempotent, so a cached option string
    can be re-seeked repeatedly without stacking ``-ss`` flags.
    """
    base = _SS_PREFIX.sub("", before_options or "").strip()
    if seek and seek > 0:
        return f"-ss {seek} {base}".strip()
    return base


def make_stream_source(input_url, before_options, options, temp_file_path=None):
    """Create an FFmpeg source and tag it so playback can be re-created later.

    The tags let seek and EQ changes rebuild FFmpeg against the already
    resolved stream instead of re-running provider search and extraction,
    which is what made the +/-10s buttons take seconds to respond.
    """
    source = discord.FFmpegPCMAudio(input_url, before_options=before_options, options=options)
    source.temp_file_path = temp_file_path
    source.stream_input = input_url
    source.stream_before_options = apply_seek(before_options, None)
    return source


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
