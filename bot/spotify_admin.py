"""Server-side helper used by spotify_pair.bat.

    python -m bot.spotify_admin import <discord_user_id> <spotify-login.zip>

Reads the Soloist key from stdin (never from the command line), checks that
the paired login works on this server, and saves it for that Discord user, so
they can pick Spotify in /provider right away.
"""
import asyncio
import sys
from pathlib import Path

from .soloist_player import SoloistManager, valid_key
from .spotify_connect import SpotifyError, UserStore


async def _import(user_id: int, zip_path: Path, key: str) -> int:
    manager = SoloistManager(UserStore())
    try:
        await manager.login_with_pairing_file(user_id, key, zip_path.read_bytes())
    except SpotifyError as e:
        print(f"FAILED: {e}")
        return 1
    print("OK: Spotify is connected for this Discord account.")
    return 0


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] != "import" or not sys.argv[2].isdigit():
        print(__doc__)
        return 2
    key = sys.stdin.readline().strip()
    if not valid_key(key):
        print("FAILED: that doesn't look like a Soloist key (it starts with spak_).")
        return 1
    return asyncio.run(_import(int(sys.argv[2]), Path(sys.argv[3]), key))


if __name__ == "__main__":
    sys.exit(main())
