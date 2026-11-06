#!/usr/bin/env python3
"""Start, stop and check the music bot on your server without logging in.

First run asks for your server details (host/IP, user, SSH key or password,
bot folder) and saves them to botctl.json next to this file. That file is in
.gitignore and never leaves your computer. Passwords are never saved: you are
asked for them each time.

    python botctl.py            # menu
    python botctl.py start      # or: stop | restart | status | logs | setup

"stop" shuts down the whole bot stack, not just the bot: the Discord bot, the
YouTube resolver, the PO token server, the cookie refresher (and any headless
Chromium it started), FFmpeg streams and, optionally, Cloudflare WARP. It then
scans for anything bot-related that is still alive, kills it, and reports the
server's memory.

Needs paramiko (already in requirements.txt):  pip install paramiko
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is missing. Install it with:  pip install paramiko")

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "botctl.json"

DEFAULTS = {
    "host": "",
    "port": 22,
    "username": "",
    "auth": "key",          # "key" or "password"
    "key_path": "",
    "bot_dir": "",           # e.g. /home/<user>/dcBot
    "stop_warp": True,       # WARP only serves the bot's YouTube traffic
}


# --------------------------------------------------------------- config ----

def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    value = input(f"{prompt}{shown}: ").strip()
    return value or default


def setup(cfg: dict | None = None) -> dict:
    cfg = {**DEFAULTS, **(cfg or {})}
    print("\nServer details (saved to botctl.json, passwords are never saved)\n")
    cfg["host"] = ask("Server IP or domain", cfg["host"])
    cfg["port"] = int(ask("SSH port", str(cfg["port"])))
    cfg["username"] = ask("SSH username", cfg["username"])
    auth = ask("Login with 'key' or 'password'", cfg["auth"]).lower()
    cfg["auth"] = "password" if auth.startswith("p") else "key"
    if cfg["auth"] == "key":
        cfg["key_path"] = ask("Path to SSH private key", cfg["key_path"])
    cfg["bot_dir"] = ask("Bot folder on the server",
                         cfg["bot_dir"] or f"/home/{cfg['username']}/dcBot")
    warp = ask("Also stop Cloudflare WARP on stop? (y/n)",
               "y" if cfg["stop_warp"] else "n")
    cfg["stop_warp"] = warp.lower().startswith("y")
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\nSaved to {CONFIG.name}.\n")
    return cfg


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            print("botctl.json is unreadable; let's set it up again.")
    return setup()


# ------------------------------------------------------------------ ssh ----

class Server:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.password = ""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(hostname=cfg["host"], port=cfg["port"],
                      username=cfg["username"], timeout=20)
        if cfg["auth"] == "password":
            self.password = getpass.getpass(f"Password for {cfg['username']}@{cfg['host']}: ")
            kwargs["password"] = self.password
        else:
            key = Path(cfg["key_path"]).expanduser()
            if not key.is_absolute():
                key = HERE / key
            kwargs["key_filename"] = str(key)
        print(f"Connecting to {cfg['host']}.")
        self.client.connect(**kwargs)

    def sudo_script(self, script: str, timeout: int = 120) -> str:
        """Run a bash script as root. Passwordless sudo, or the login password."""
        cmd = "sudo -S -p '' bash -s" if self.password else "sudo -n bash -s"
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        if self.password:
            stdin.write(self.password + "\n")
        stdin.write(script)
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if self.password:
            out, err = out.replace(self.password, "***"), err.replace(self.password, "***")
        if "a password is required" in err:
            err += "\n(sudo needs a password: run setup and choose 'password' login.)"
        return (out + ("\n" + err.strip() if err.strip() else "")).rstrip()

    def close(self):
        self.client.close()


# -------------------------------------------------------------- scripts ----
# Every bot-related process is matched by what it runs, so other apps on the
# same server (game servers, websites, other Node/Python apps) are left alone.

def _header(cfg: dict) -> str:
    return f'''set -u
BOT_DIR={json.dumps(cfg["bot_dir"])}
BOT_USER={json.dumps(cfg["username"])}
STOP_WARP={"1" if cfg["stop_warp"] else "0"}
bot_pids() {{
  # Discord bot, resolver, cookie refresher, PO token server, Playwright
  # Chromium, and FFmpeg streams that go through the bot's proxy.
  pgrep -f -- "$BOT_DIR/dc.py|$BOT_DIR/ytjs/|cookie_refresher.mjs|bgutil-ytdlp-pot-provider|ms-playwright/.*(chrome|headless_shell)|ffmpeg.*127\\.0\\.0\\.1:40000|ffmpeg.*googlevideo" | grep -vx "$$" || true
}}
show_procs() {{
  local pids; pids=$(bot_pids | tr '\\n' ',' | sed 's/,$//')
  if [ -n "$pids" ]; then
    ps -o rss=,args= -p "$pids" | awk '{{mb=$1/1024; $1=""; n=$0; if (n ~ /dc[.]py/) n="Discord bot"; else if (n ~ /server[.]mjs/) n="YouTube resolver"; else if (n ~ /cookie_refresher/) n="Cookie refresher"; else if (n ~ /bgutil/) n="PO token server"; else if (n ~ /ffmpeg/) n="FFmpeg stream"; else if (n ~ /chrom|headless/) n="Chromium"; printf "  %s, %d MB\\n", n, mb}}'
  else
    echo "  none"
  fi
}}
mem() {{ free -m | awk '/Mem:/{{printf "Server memory: %d MB used, %d MB free, out of %d MB\\n", $3, $7, $2}}'; }}
'''


def script_status(cfg: dict) -> str:
    return _header(cfg) + '''
echo "Services:"
supervisorctl status 2>/dev/null | grep -E "dcbot|bgutil" | awk '{n=$1; if(n=="dcbot")n="Discord bot"; else if(n=="dcbot-ytjs")n="YouTube resolver"; else if(n=="bgutil-pot")n="PO token server"; printf "  %s: %s\\n", n, tolower($2)}' || echo "  supervisor not found"
printf "  Cookie timer: %s\\n" "$(systemctl is-active dcbot-cookies.timer 2>/dev/null)"
if command -v warp-cli >/dev/null; then
  w=$(warp-cli --accept-tos status 2>/dev/null | head -1 | sed 's/^Status update: //' | tr 'A-Z' 'a-z')
  printf "  WARP: %s\\n" "${w:-stopped}"
fi
echo "Bot processes:"
show_procs
mem
'''


def script_stop(cfg: dict) -> str:
    return _header(cfg) + '''
echo "Stopping the bot and everything it uses."
supervisorctl stop dcbot dcbot-ytjs bgutil-pot >/dev/null 2>&1 || true
systemctl stop dcbot-cookies.timer dcbot-cookies.service >/dev/null 2>&1 || true
# Older installs ran the cookie refresher as a long-running service.
systemctl stop dcbot-cookies >/dev/null 2>&1 || true

# Anything still alive (orphaned FFmpeg, Chromium, a bot started by hand...)
left=$(bot_pids)
if [ -n "$left" ]; then
  echo "Ending leftover bot processes."
  kill $left 2>/dev/null || true
  sleep 3
  left=$(bot_pids)
  [ -n "$left" ] && kill -9 $left 2>/dev/null || true
fi

if [ "$STOP_WARP" = "1" ] && systemctl list-unit-files warp-svc.service >/dev/null 2>&1; then
  warp-cli --accept-tos disconnect >/dev/null 2>&1 || true
  systemctl stop warp-svc >/dev/null 2>&1 || true
  echo "Cloudflare WARP stopped."
fi
sleep 1
echo "Bot processes still running:"
show_procs
[ -z "$(bot_pids)" ] && echo "All clear: no bot, Node.js, Chromium or FFmpeg process of the bot is running."
mem
'''


def script_start(cfg: dict) -> str:
    return _header(cfg) + '''
echo "Starting the bot."
if systemctl list-unit-files warp-svc.service >/dev/null 2>&1; then
  systemctl start warp-svc >/dev/null 2>&1 || true
  sleep 2
  warp-cli --accept-tos connect >/dev/null 2>&1 || true
fi
supervisorctl start dcbot-ytjs >/dev/null 2>&1 || true
sleep 2
supervisorctl start dcbot >/dev/null 2>&1 || true
systemctl start dcbot-cookies.timer >/dev/null 2>&1 || true
echo "Waiting for the bot to come online."
for i in $(seq 1 20); do
  sleep 2
  tail -n 5 "$BOT_DIR/music_bot.log" 2>/dev/null | grep -q "logged in as" && break
done
name=$(tail -n 30 "$BOT_DIR/music_bot.log" 2>/dev/null | grep "logged in as" | tail -1 | sed -E 's/.*logged in as ([^ ]+).*/\\1/')
if [ -n "$name" ]; then echo "Bot is online as $name."; else echo "Bot has not logged in yet. Check Status in a minute."; fi
echo "Bot processes:"
show_procs
mem
'''


def script_logs(cfg: dict) -> str:
    return _header(cfg) + '''
echo "Bot log, last 25 lines:"
tail -n 25 "$BOT_DIR/music_bot.log" 2>/dev/null | cut -c1-180
echo "Recent errors:"
grep -E "ERROR|Traceback" "$BOT_DIR/music_bot.log" 2>/dev/null | tail -n 10 | cut -c1-180
'''


ACTIONS = {
    "start": script_start,
    "stop": script_stop,
    "status": script_status,
    "logs": script_logs,
}


# ----------------------------------------------------------------- main ----

def run(action: str, cfg: dict) -> None:
    server = Server(cfg)
    try:
        if action == "restart":
            print(server.sudo_script(script_stop(cfg)))
            print()
            print(server.sudo_script(script_start(cfg)))
        else:
            print(server.sudo_script(ACTIONS[action](cfg)))
    finally:
        server.close()


def menu(cfg: dict) -> None:
    options = ["start", "stop", "restart", "status", "logs", "setup", "quit"]
    while True:
        print(f"\nMusic bot on {cfg['host']}")
        for i, name in enumerate(options, 1):
            print(f"  {i}. {name}")
        choice = input("Choose: ").strip().lower()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            choice = options[int(choice) - 1]
        if choice in ("quit", "q", "exit"):
            return
        if choice == "setup":
            cfg = setup(cfg)
            continue
        if choice in ACTIONS or choice == "restart":
            try:
                run(choice, cfg)
            except Exception as e:  # connection/auth problems
                print(f"Error: {e}")
        else:
            print("Unknown choice.")


def main() -> None:
    args = [a.lower() for a in sys.argv[1:]]
    if args and args[0] == "setup":
        setup(json.loads(CONFIG.read_text()) if CONFIG.exists() else None)
        return
    cfg = load_config()
    if not args:
        menu(cfg)
    elif args[0] in ACTIONS or args[0] == "restart":
        run(args[0], cfg)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
