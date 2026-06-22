import os
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

AUDIO_DIR = os.path.abspath("audio")
SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".mp4"}

class LocalFileHandler:
    @classmethod
    def get_audio_dir(cls) -> str:
        # Create audio directory if it doesn't exist
        if not os.path.exists(AUDIO_DIR):
            os.makedirs(AUDIO_DIR, exist_ok=True)
        return AUDIO_DIR

    @classmethod
    def is_safe_path(cls, path: str) -> bool:
        """
        Verify that a path is safe and stays inside the audio directory.
        Prevents directory traversal attacks.
        """
        try:
            target_abs = os.path.abspath(path)
            common = os.path.commonpath([AUDIO_DIR, target_abs])
            return common == AUDIO_DIR
        except Exception as e:
            logger.error(f"Error checking path safety: {e}")
            return False

    @classmethod
    def list_files(cls) -> List[str]:
        """
        Scan the audio directory recursively for supported audio files.
        Returns a list of file paths relative to the audio directory.
        """
        audio_dir = cls.get_audio_dir()
        file_list = []
        for root, _, files in os.walk(audio_dir):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    abs_path = os.path.join(root, file)
                    if cls.is_safe_path(abs_path):
                        rel_path = os.path.relpath(abs_path, audio_dir)
                        # Replace windows backslash with forward slash for consistency in discord
                        file_list.append(rel_path.replace("\\", "/"))
        return sorted(file_list)

    @classmethod
    def get_absolute_path(cls, rel_path: str) -> Optional[str]:
        """
        Get the sanitized, verified absolute path for a relative path.
        Returns None if the path is unsafe or doesn't exist.
        """
        audio_dir = cls.get_audio_dir()
        target_path = os.path.join(audio_dir, rel_path)
        if cls.is_safe_path(target_path) and os.path.exists(target_path) and os.path.isfile(target_path):
            return os.path.abspath(target_path)
        return None
