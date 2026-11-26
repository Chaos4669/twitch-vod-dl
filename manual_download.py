"""Download a single completed Twitch VOD without retention cleanup."""

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

import downloader

LOG = logging.getLogger(__name__)
OUTPUT_DIRECTORY = Path("/vods")


def vod_id_from_url(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r"/videos/([0-9]+)/?", parsed.path)
    if (parsed.scheme not in ("http", "https")
            or parsed.netloc.lower() not in ("twitch.tv", "www.twitch.tv", "m.twitch.tv")
            or not match):
        raise ValueError("Expected a Twitch VOD URL: https://www.twitch.tv/videos/1234567890")
    return match[1]


def download_url(url, directory, quality):
    vod_id = vod_id_from_url(url)
    video = json.loads(downloader.run(
        ["twitch-dl", "info", vod_id, "--json"], capture=True, timeout=120,
    ))
    if not isinstance(video, dict) or str(video.get("id")) != vod_id:
        raise ValueError("Twitch returned unexpected VOD metadata")
    if video.get("status") != "RECORDED":
        raise ValueError("The VOD must have finished recording before downloading")
    directory.mkdir(parents=True, exist_ok=True)
    downloader.download_video(video, directory, quality, temp_root=directory / "tmp")
    LOG.info("Manual VOD stored in %s; automatic cleanup does not touch this directory", directory)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2:
        LOG.error("Usage: tdl <twitch-vod-url>")
        return 1
    try:
        download_url(sys.argv[1], OUTPUT_DIRECTORY, os.environ.get("VOD_QUALITY", ""))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        LOG.error("Manual download failed: %s", error)
        return 1
    except KeyboardInterrupt:
        LOG.info("Manual download interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
