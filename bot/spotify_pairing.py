"""Log Soloist in from the server alone, with a Spotify pairing code.

Spotify Soloist only logs in through Spotify Connect: normally the Spotify
app on the same Wi-Fi finds it and sends it the login. The server isn't on
the user's Wi-Fi, so the bot does the app's part itself:

1. Spotify's own pairing-code login (spotify.com/pair) gives the bot a token
   once the user approves on their phone or browser.
2. The bot sends that login to Soloist's local Connect endpoint (the same
   "addUser" request the Spotify app sends), and Soloist stores it.

The addUser encryption follows librespot's discovery code.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
from urllib.parse import urlencode

import logging

import httpx
from Crypto.Cipher import AES

from .spotify_connect import PremiumRequired, SpotifyError

# Spotify's desktop client ID, as used by librespot for Connect logins.
logger = logging.getLogger(__name__)

CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
DEVICE_AUTH_URL = "https://accounts.spotify.com/oauth2/device/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

_DH_PRIME = int(
    "ffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74"
    "020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f1437"
    "4fe1356d6d51c245e485b576625e7ec6f44c42e9a63a3620ffffffffffffffff", 16)
_IV = bytes([253, 81, 222, 19, 70, 203, 45, 89, 141, 68, 210, 240, 93, 20, 76, 30])
AUTH_SPOTIFY_TOKEN = 3


def _be(n: int) -> bytes:
    return n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")


# ------------------------------------------------------------ pairing code --

async def start_code() -> dict:
    """Returns device_code, user_code, verification_uri_complete, interval, expires_in."""
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(DEVICE_AUTH_URL, data={
            "client_id": CLIENT_ID, "scope": "streaming",
            "description": "Discord Music Bot",
        })
    if r.status_code != 200:
        raise SpotifyError(f"Spotify didn't give a pairing code ({r.status_code}). Try again in a minute.")
    return r.json()


async def wait_for_token(code: dict) -> dict:
    """Poll until the user approves the code. Returns Spotify's token response."""
    interval = max(int(code.get("interval", 5)), 2)
    deadline = asyncio.get_running_loop().time() + min(int(code.get("expires_in", 600)), 900)
    async with httpx.AsyncClient(timeout=20) as client:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)
            r = await client.post(TOKEN_URL, data={
                "client_id": CLIENT_ID, "device_code": code["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and data.get("access_token"):
                logger.info("Spotify pairing token fields: %s", sorted(data))
                return data
            err = data.get("error", "")
            if err == "slow_down":
                interval += 2
            elif err in ("authorization_pending", ""):
                continue
            elif err in ("access_denied",):
                raise SpotifyError("The Spotify login was cancelled. Run `/spotify login` again.")
            elif err in ("expired_token",):
                break
            else:
                raise SpotifyError(f"Spotify login failed ({err}). Run `/spotify login` again.")
    raise SpotifyError("The pairing code expired. Run `/spotify login` again.")


async def account(token: str) -> dict:
    """The user's Spotify account, or {} if Spotify is rate limiting.

    The account name is optional for the Connect login, so a busy Spotify
    API (429) must not stop the login.
    """
    async with httpx.AsyncClient(timeout=20) as client:
        waited = 0.0
        while True:
            r = await client.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 200:
                me = r.json()
                if me.get("product") and me["product"] != "premium":
                    raise PremiumRequired()
                return me
            logger.info("Spotify /v1/me answered %s (Retry-After %s)", r.status_code, r.headers.get("Retry-After"))
            if r.status_code != 429:
                break
            try:
                wait = max(float(r.headers.get("Retry-After", "3")), 1)
            except ValueError:
                wait = 3
            if waited + wait > 60:
                break
            await asyncio.sleep(wait)
            waited += wait
    return {}


# ---------------------------------------------------------- Connect login --

def _write_int(n: int, out: bytearray) -> None:
    if n < 0x80:
        out.append(n)
    else:
        out.append(0x80 | (n & 0x7F))
        out.append(n >> 7)


def _write_bytes(b: bytes, out: bytearray) -> None:
    _write_int(len(b), out)
    out += b


def build_blob(username: str, auth_type: int, auth_data: bytes, device_id: str) -> str:
    blob = bytearray()
    _write_int(0x49, blob)
    _write_bytes(username.encode(), blob)
    _write_int(0x50, blob)
    _write_int(auth_type, blob)
    _write_int(0x51, blob)
    _write_bytes(auth_data, blob)
    n_zeros = 16 - (len(blob) % 16) - 1
    blob += bytes(n_zeros)
    blob.append(n_zeros + 1)
    for j in range(16, len(blob)):
        blob[j] ^= blob[j - 16]
    secret = hashlib.sha1(device_id.encode()).digest()
    k = hashlib.pbkdf2_hmac("sha1", secret, username.encode(), 0x100, 20)
    key = hashlib.sha1(k).digest() + b"\x00\x00\x00" + bytes([20])
    return base64.b64encode(AES.new(key, AES.MODE_ECB).encrypt(bytes(blob))).decode()


def encrypt_blob(blob: str, private: int, remote_key_b64: str) -> str:
    remote = int.from_bytes(base64.b64decode(remote_key_b64), "big")
    shared = _be(pow(remote, private, _DH_PRIME))
    base_key = hashlib.sha1(shared).digest()[:16]
    checksum_key = hmac.new(base_key, b"checksum", hashlib.sha1).digest()
    enc_key = hmac.new(base_key, b"encryption", hashlib.sha1).digest()[:16]
    data = AES.new(enc_key, AES.MODE_CTR, nonce=b"", initial_value=_IV).encrypt(blob.encode())
    mac = hmac.new(checksum_key, data, hashlib.sha1).digest()
    return base64.b64encode(_IV + data + mac).decode()


async def add_user(port: int, username: str, token: str, path: str = "/zc") -> None:
    """Send the login to the Soloist Connect endpoint on this server."""
    base = f"http://127.0.0.1:{port}{path}"
    async with httpx.AsyncClient(timeout=20) as client:
        info = (await client.get(base, params={"action": "getInfo"})).json()
        blob = build_blob(username, AUTH_SPOTIFY_TOKEN, token.encode(), info["deviceID"])
        private = int.from_bytes(os.urandom(95), "big")
        public = _be(pow(2, private, _DH_PRIME))
        body = {
            "action": "addUser",
            "userName": username,
            "blob": encrypt_blob(blob, private, info["publicKey"]),
            "clientKey": base64.b64encode(public).decode(),
            "deviceId": hashlib.sha1(b"discord-music-bot").hexdigest(),
            "deviceName": "Discord Music Bot",
            "loginId": secrets.token_hex(16),
        }
        if info.get("tokenType"):
            body["tokenType"] = info["tokenType"]
        r = await client.post(base, content=urlencode(body),
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        status = r.json().get("statusString", "")
    except ValueError:
        status = r.text[:120]
    if status not in ("OK", "ERROR-OK"):
        raise SpotifyError(f"Spotify's player didn't accept the login ({status}). Run `/spotify login` again.")
