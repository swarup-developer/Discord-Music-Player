from __future__ import annotations

import logging
import os
import platform
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

logger = logging.getLogger(__name__)
OPUS_CACHE_DIR = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir())) / "dcBot" / "opus"
OPUS_DLL_NAMES = ("libopus.dll", "libopus-0.dll", "opus.dll")
OPUS_DOWNLOADS = {
    "win64": [
        "https://archive.mozilla.org/pub/opus/win64/opus-tools-0.2-win64.zip",
        "https://archive.mozilla.org/pub/opus/win64/opus-tools-0.2-win64.zip",
    ],
    "win32": [
        "https://archive.mozilla.org/pub/opus/win32/opus-tools-0.2-win32.zip",
        "https://archive.mozilla.org/pub/opus/win32/opus-tools-0.2-win32.zip",
    ],
}


def _arch_key() -> str:
    return "win64" if platform.architecture()[0] == "64bit" else "win32"


def _candidate_paths() -> list[Path]:
    candidates = []
    for name in OPUS_DLL_NAMES:
        candidates.append(Path(name))
        candidates.append(OPUS_CACHE_DIR / name)
    return candidates


def _load_candidate(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, ""
    try:
        import discord

        discord.opus.load_opus(str(path))
        return discord.opus.is_loaded(), f"file:{path}"
    except Exception:
        return False, ""


def _download_zip(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url, timeout=30) as response:
        data = response.read()
    destination.write_bytes(data)


def _extract_opus_dll(zip_path: Path, target_dir: Path) -> Path | None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            if member.lower().endswith(".dll") and "opus" in Path(member).name.lower():
                extracted = archive.extract(member, path=target_dir)
                extracted_path = Path(extracted)
                final_path = target_dir / extracted_path.name
                if extracted_path != final_path:
                    final_path.write_bytes(extracted_path.read_bytes())
                    extracted_path.unlink(missing_ok=True)
                return final_path
    return None


def ensure_opus_loaded() -> bool:
    try:
        import discord
    except Exception:
        return False

    if discord.opus.is_loaded():
        logger.info("Opus already loaded before bootstrap")
        return True

    if os.name != "nt":
        for name in ("libopus.so.0", "libopus.so", "opus"):
            try:
                discord.opus.load_opus(name)
                if discord.opus.is_loaded():
                    logger.info("Opus loaded on non-Windows platform using name: %s", name)
                    return True
            except Exception:
                continue
        return False

    try:
        discord_bin = Path(discord.__file__).resolve().parent / "bin"
        for name in ("libopus-0.x64.dll", "libopus-0.x86.dll"):
            loaded, source = _load_candidate(discord_bin / name)
            if loaded:
                logger.info("Opus loaded from bundled Discord wheel DLL (%s)", source)
                return True
    except Exception:
        pass

    for candidate in _candidate_paths():
        loaded, source = _load_candidate(candidate)
        if loaded:
            logger.info("Opus loaded from local candidate DLL (%s)", source)
            return True

    arch = _arch_key()
    download_urls = OPUS_DOWNLOADS[arch]
    zip_path = OPUS_CACHE_DIR / f"opus-tools-{arch}.zip"

    for url in download_urls:
        try:
            _download_zip(url, zip_path)
            extracted = _extract_opus_dll(zip_path, OPUS_CACHE_DIR)
            if extracted:
                loaded, source = _load_candidate(extracted)
                if loaded:
                    logger.info("Opus downloaded and loaded from cached DLL (%s)", source)
                    return True
        except Exception:
            continue

    return False
