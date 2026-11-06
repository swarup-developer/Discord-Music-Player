"""Server-side helper used by spotify_pair.bat.

    python -m bot.spotify_admin pair <discord_user_id>
    python -m bot.spotify_admin import <discord_user_id> <spotify-login.zip>

Reads the Soloist key from stdin (never from the command line).

"pair" starts Spotify pairing here on the server and prints ZC_PORT=<port>
so the helper on the user's PC can make this server show up in their
Spotify app (it relays the connection through SSH). The login is stored on
this server, then checked and saved for that Discord user.
"""
import asyncio
import logging
import re
import sys
import traceback
from pathlib import Path

from .soloist_player import SoloistManager, valid_key
from .spotify_connect import SpotifyError, UserStore

LOG = Path(__file__).resolve().parent.parent / "spotify_admin.log"


async def _run(job) -> int:
    try:
        await job
    except SpotifyError as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    except Exception as e:  # keep the details for the bot owner, show one line
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        print(f"FAILED: {type(e).__name__}: {e} (details in spotify_admin.log on the server)", flush=True)
        return 1
    print("OK: Spotify is connected for this Discord account.", flush=True)
    return 0


async def _pair(user_id: int, key: str) -> None:
    manager = SoloistManager(UserStore())
    sent = False

    def on_line(text: str) -> None:
        nonlocal sent
        m = re.search(r"ZeroConf service on port (\d+)", text)
        if m and not sent:
            sent = True
            print(f"ZC_PORT={m.group(1)}", flush=True)

    async def on_step(text, url):
        print(f"OPEN: {url}", flush=True)

    await manager.login(user_id, key, on_step, on_line=on_line, fresh=True)
    print("PAIRED", flush=True)
    await manager.verify_and_save(user_id, key)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = sys.argv[1:]
    ok = (len(args) == 2 and args[0] == "pair" and args[1].isdigit()) or (
        len(args) == 3 and args[0] == "import" and args[1].isdigit())
    if not ok:
        print(__doc__)
        return 2
    key = sys.stdin.readline().strip()
    if not valid_key(key):
        print("FAILED: that doesn't look like a Soloist key (it starts with spak_).")
        return 1
    user_id = int(args[1])
    if args[0] == "pair":
        return asyncio.run(_run(_pair(user_id, key)))
    manager = SoloistManager(UserStore())
    return asyncio.run(_run(manager.login_with_pairing_file(user_id, key, Path(args[2]).read_bytes())))


if __name__ == "__main__":
    sys.exit(main())
