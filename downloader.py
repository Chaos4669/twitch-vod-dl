"""Continuously download and retain the latest Twitch VODs."""

import json
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading

CHECK_INTERVAL = 3600
MAX_SECONDS = 10800
STORAGE_ROOT = Path("/vods")
VOD_FILENAME = re.compile(r"(?:^|_)([0-9]+)\.mp4$", re.IGNORECASE)
EPISODE_FILENAME = re.compile(r"^S([0-9]+)E([0-9]+)_", re.IGNORECASE)
LOG = logging.getLogger(__name__)


def configuration():
    if len(sys.argv) > 1:
        raise ValueError("Launch arguments are not supported; use CHANNEL_NAME, VOD_QUALITY, VOD_COUNT and DELETE_OLD_VODS")
    channel = os.environ.get("CHANNEL_NAME", "Couriway")
    if not re.fullmatch(r"[A-Za-z0-9_]+", channel):
        raise ValueError("CHANNEL_NAME must be a Twitch channel login")
    count = os.environ.get("VOD_COUNT", "5")
    if not re.fullmatch(r"[0-9]+", count) or int(count) < 1:
        raise ValueError("VOD_COUNT must be a positive integer")
    delete_old_vods = os.environ.get("DELETE_OLD_VODS", "true").strip().lower()
    if delete_old_vods not in ("true", "false"):
        raise ValueError("DELETE_OLD_VODS must be true or false")
    return channel, os.environ.get("VOD_QUALITY", ""), int(count), delete_old_vods == "true"


def run(command, *, capture=False, timeout=None):
    with subprocess.Popen(
        command, text=True, encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            # Stop the child before deleting its temporary download directory.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output)
        return output


def fetch_videos(channel, count):
    payload = json.loads(run([
        "twitch-dl", "videos", channel, "--limit", str(count),
        "--sort", "time", "--type", "archive", "--json",
    ], capture=True, timeout=120))
    videos = payload["videos"]
    for video in videos:
        if not re.fullmatch(r"[0-9]+", str(video["id"])):
            raise ValueError("Invalid VOD ID returned by Twitch")
        if video["status"] not in ("RECORDED", "RECORDING"):
            raise ValueError(f"Unexpected VOD status: {video['status']}")
    # Limit before filtering: a live VOD occupies one of the configured slots.
    selected = videos[:count]
    for video in selected:
        if video["status"] == "RECORDING":
            LOG.info("Skipping currently recording VOD %s", video["id"])
    return [video for video in selected if video["status"] != "RECORDING"]


def saved_vods(directory):
    groups = {}
    for path in directory.iterdir():
        match = VOD_FILENAME.search(path.name)
        if path.is_file() and match:
            groups.setdefault(match[1], []).append(path)
    return groups


def safe_title(title):
    title = title.replace("/", "-").replace("\\", "-")
    title = re.sub(r"[^\w .()\[\]-]", "", title)
    return re.sub(r"\s+", " ", title).strip()[:20].strip()


def stream_timestamp(video):
    value = video.get("recordedAt")
    if not isinstance(value, str):
        raise ValueError(f"Missing recording start time for VOD {video['id']}")
    started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if started.tzinfo is None:
        raise ValueError(f"Recording start time has no timezone for VOD {video['id']}")
    return started.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%MZ")


def next_season(directory):
    """Use the highest season in this output directory plus one, or one if empty."""
    highest = 0
    for paths in saved_vods(directory).values():
        for path in paths:
            match = EPISODE_FILENAME.match(path.name)
            if match:
                highest = max(highest, int(match[1]))
    return highest + 1


def download_video(video, directory, quality, *, temp_root=None):
    vod_id = str(video["id"])
    existing = saved_vods(directory).get(vod_id, [])
    if existing and all(path.is_file() and path.stat().st_size > 0 for path in existing):
        LOG.info("VOD %s already exists; skipping", vod_id)
        return
    title = safe_title(video.get("title") or "")
    title_suffix = f"_{title}" if title else ""
    timestamp = stream_timestamp(video)
    # Publish only complete downloads/parts. Temporary files are removed when
    # a command fails and cannot be mistaken for a saved VOD on the next check.
    if temp_root is None:
        temp_root = directory.parent.parent / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{vod_id}-", dir=temp_root) as work:
        work = Path(work)
        cache = work / "cache"
        cache.mkdir()
        original = work / "original.mp4"
        command = [
            "twitch-dl", "download", vod_id, "--output", str(original),
            "--format", "mp4", "--cache-dir", str(cache), "--max-workers", "8",
            "--overwrite", "--skip-live", "--quality", quality or "source",
        ]
        LOG.info("Downloading VOD %s (%s)", vod_id, title)
        run(command)
        if not original.is_file() or original.stat().st_size == 0:
            raise ValueError(f"VOD {vod_id} produced no completed download")
        duration = float(run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(original),
        ], capture=True).strip())
        if duration <= MAX_SECONDS:
            original.rename(work / "Teil1.mp4")
        else:
            pattern = work / "Teil%d.mp4"
            run([
                "ffmpeg", "-y", "-i", str(original), "-c", "copy", "-map", "0",
                "-f", "segment", "-segment_time", str(MAX_SECONDS),
                "-reset_timestamps", "1", "-segment_start_number", "1", str(pattern),
            ])
            original.unlink()
        parts = list(work.glob("*.mp4"))
        if not parts or any(path.stat().st_size == 0 for path in parts):
            raise ValueError(f"VOD {vod_id} produced no complete video parts")
        season = next_season(directory)
        parts.sort(key=lambda path: int(path.stem[4:]))
        published = []
        try:
            for path in existing:
                path.unlink(missing_ok=True)
            for path in parts:
                episode = int(path.stem[4:])
                destination = directory / f"S{season:03d}E{episode:02d}_{timestamp}{title_suffix}_{vod_id}.mp4"
                published.append(destination)
                path.replace(destination)
        except BaseException:
            # A failed/interrupted publication must not leave some parts that
            # would make the next check skip an incomplete VOD.
            for path in published:
                path.unlink(missing_ok=True)
            raise
    LOG.info("Saved VOD %s (%s parts)", vod_id, len(parts))


def cleanup(directory, count):
    groups = saved_vods(directory)
    # VOD IDs follow creation order; download times would rank backfills newest.
    for vod_id in sorted(groups, key=int, reverse=True)[count:]:
        LOG.info("Deleting older VOD %s", vod_id)
        for path in groups[vod_id]:
            path.unlink(missing_ok=True)


def check_once(channel, quality, count, directory, *, delete_old_vods=True):
    LOG.info("Checking latest %s VODs for %s", count, channel)
    videos = fetch_videos(channel, count)
    failed = False
    # Assign seasons to new VODs oldest first within the selected window.
    for video in sorted(videos, key=lambda video: int(video["id"])):
        try:
            download_video(video, directory, quality)
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.exception("Download failed for VOD %s; retrying next check", video["id"])
            failed = True
    # Preserve older usable VODs while their replacements failed.
    if not delete_old_vods:
        LOG.info("Skipping cleanup because DELETE_OLD_VODS=false")
    elif not failed:
        cleanup(directory, count)
    else:
        LOG.warning("Skipping cleanup because a download failed")


def serve(channel, quality, count, directory, stop, *, delete_old_vods=True):
    while not stop.is_set():
        try:
            check_once(channel, quality, count, directory, delete_old_vods=delete_old_vods)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            LOG.exception("VOD check failed; retrying in one hour")
        LOG.info("Next VOD check in one hour")
        stop.wait(CHECK_INTERVAL)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        channel, quality, count, delete_old_vods = configuration()
    except ValueError as error:
        LOG.error("%s", error)
        return 1
    directory = STORAGE_ROOT / "VODs" / channel
    directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def shutdown(signum, frame):
        LOG.info("Stopping downloader")
        stop.set()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("Starting Twitch VOD Downloader %s for %s (VOD_COUNT=%s, DELETE_OLD_VODS=%s)",
             os.environ.get("APP_VERSION", "unknown"), channel, count, delete_old_vods)
    serve(channel, quality, count, directory, stop, delete_old_vods=delete_old_vods)
    return 0


if __name__ == "__main__":
    sys.exit(main())
