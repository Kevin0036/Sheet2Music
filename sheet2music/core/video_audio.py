"""Video URL to audio adapter for YouTube and Bilibili."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "bilibili.com", "www.bilibili.com", "m.bilibili.com"}


def validate_video_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("只支持 YouTube 或 Bilibili 的 http(s) 视频 URL")
    return url


def yt_dlp_binary() -> str:
    binary = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not binary:
        raise FileNotFoundError("找不到 yt-dlp，请安装 yt-dlp 并加入 PATH")
    return binary


def build_ytdlp_command(binary: str, url: str, target: Path) -> list[str]:
    template = str(target.with_suffix("")) + ".%(ext)s"
    return [binary, "--no-playlist", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0", "--output", template, url]


def download_video_audio(url: str, target: Path) -> Path:
    validated = validate_video_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(build_ytdlp_command(yt_dlp_binary(), validated, target), check=True)
    candidates = [target, *target.parent.glob(f"{target.stem}.*")]
    source = next((path for path in candidates if path.is_file() and path.suffix.lower() == ".mp3"), None)
    if source is None:
        raise FileNotFoundError(f"yt-dlp 未生成 MP3: {target}")
    if source != target:
        source.replace(target)
    return target
