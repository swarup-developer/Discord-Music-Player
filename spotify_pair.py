#!/usr/bin/env python3
"""Connect your Spotify Premium account to the music bot (bot owner helper).

Run spotify_pair.bat (or: python spotify_pair.py). It asks for your Soloist
key once, then "Discord Music Bot" shows up in the Spotify app on this
computer (or on your phone on the same Wi-Fi). Pick it and you're done.

How it works: Spotify only pairs players on your own network, so this
starts the bot's player on the server, makes it appear on your network,
and passes the pairing through your SSH connection. The login is stored on
the server; nothing is kept on this computer.

Needs: botctl.json (server details), paramiko and zeroconf.
"""
from __future__ import annotations

import json
import select
import socket
import sys
import threading
import time
from pathlib import Path

try:
    import paramiko
    import ifaddr
    from zeroconf import ServiceInfo, Zeroconf
except ImportError:
    sys.exit("Missing packages. Install them with:  pip install paramiko zeroconf")

HERE = Path(__file__).resolve().parent
NAME = "Discord Music Bot"


def lan_ips() -> list[str]:
    ips = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                ips.append(ip.ip)
    return ips or ["127.0.0.1"]


def relay(transport: paramiko.Transport, remote_port: int, stop: threading.Event) -> int:
    """Listen on this PC and pass each connection to the server's pairing port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 0))
    server.listen(8)
    server.settimeout(0.5)

    def pipe(client: socket.socket):
        try:
            chan = transport.open_channel("direct-tcpip", ("127.0.0.1", remote_port), client.getpeername())
        except Exception:
            client.close()
            return
        try:
            while not stop.is_set():
                r, _, _ = select.select([client, chan], [], [], 0.5)
                if client in r:
                    data = client.recv(65536)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
        except OSError:
            pass
        finally:
            chan.close()
            client.close()

    def accept_loop():
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=pipe, args=(client,), daemon=True).start()
        server.close()

    threading.Thread(target=accept_loop, daemon=True).start()
    return server.getsockname()[1]


def main() -> int:
    cfg_path = HERE / "botctl.json"
    if not cfg_path.exists():
        print("botctl.json is missing. Run botctl.bat once and enter your server details.")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    duid = str(cfg.get("discord_user_id", "")).strip()
    if not duid.isdigit():
        duid = input("Your Discord user ID (numbers only): ").strip()
        if not duid.isdigit():
            return 1
        cfg["discord_user_id"] = duid
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    key = input("Paste your Spotify key (starts with spak_) and press Enter: ").strip()
    if not key.startswith("spak_"):
        print("That doesn't look like a Soloist key.")
        return 1

    key_file = Path(cfg.get("key_path", "")).expanduser()
    if not key_file.is_absolute():
        key_file = HERE / key_file
    print("Connecting to the bot's server.")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(cfg["host"], port=int(cfg.get("port", 22)), username=cfg["username"],
                key_filename=str(key_file), timeout=20)
    transport = ssh.get_transport()
    transport.set_keepalive(20)

    bot_dir = cfg.get("bot_dir") or f"/home/{cfg['username']}/dcBot"
    stdin, stdout, _ = ssh.exec_command(
        f"cd '{bot_dir}' && .venv/bin/python -m bot.spotify_admin pair {duid} 2>&1", get_pty=False)
    stdin.write(key + "\n")
    stdin.flush()
    stdin.channel.shutdown_write()

    stop = threading.Event()
    zc = info = None
    result = 1
    try:
        for raw in iter(stdout.readline, ""):
            line = raw.strip()
            if line.startswith("ZC_PORT="):
                local_port = relay(transport, int(line.split("=", 1)[1]), stop)
                info = ServiceInfo(
                    "_spotify-connect._tcp.local.",
                    f"{NAME}._spotify-connect._tcp.local.",
                    parsed_addresses=lan_ips(), port=local_port,
                    properties={"CPath": "/zc", "VERSION": "1.0", "Stack": "SP"},
                    server=f"{socket.gethostname()}-dcbot.local.",
                )
                zc = Zeroconf()
                zc.register_service(info)
                print()
                print(f'Now open Spotify on this computer, or on your phone on the same Wi-Fi.')
                print(f'Open the device list, choose "{NAME}", and play any song.')
                print("If Windows asks to allow Python on the network, choose Allow.")
                print("Waiting for Spotify.")
            elif line == "PAIRED":
                print("Spotify connected. Checking it on the bot.")
            elif line.startswith("OPEN: "):
                print("Open this link to finish: " + line[6:])
            elif line.startswith(("OK:", "FAILED:")):
                print(line[line.index(":") + 1:].strip())
                result = 0 if line.startswith("OK:") else 1
    finally:
        stop.set()
        if zc:
            try:
                zc.unregister_service(info)
            finally:
                zc.close()
        ssh.close()
    if result == 0:
        print()
        print("All done. In Discord choose Spotify in /provider, then use /play.")
    return result


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    except Exception as e:
        print(f"Error: {e}")
        code = 1
    time.sleep(0.2)
    sys.exit(code)
