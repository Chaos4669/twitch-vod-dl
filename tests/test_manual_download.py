import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import downloader
import manual_download as manual


class ManualDownloadTests(unittest.TestCase):
    def test_url_validation(self):
        for url in ("https://www.twitch.tv/videos/123", "https://twitch.tv/videos/123/?filter=archives", "https://m.twitch.tv/videos/123"):
            self.assertEqual(manual.vod_id_from_url(url), "123")
        for url in ("123", "https://example.com/videos/123", "https://www.twitch.tv/streamer", "https://clips.twitch.tv/clip", "https://twitch.tv@evil.com/videos/123"):
            with self.assertRaises(ValueError):
                manual.vod_id_from_url(url)

    def test_manual_download_splits_in_root_and_survives_channel_cleanup(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / "vods"
            item = {"id": "123", "title": "Test stream", "recordedAt": "2026-10-14T18:30:00Z", "status": "RECORDED"}

            def fake_run(command, **kwargs):
                if command[:2] == ["twitch-dl", "info"]:
                    self.assertEqual(command, ["twitch-dl", "info", "123", "--json"])
                    return json.dumps(item)
                if command[:2] == ["twitch-dl", "download"]:
                    self.assertEqual(command[-2:], ["--quality", "720p60"])
                    cache = Path(command[command.index("--cache-dir") + 1])
                    self.assertEqual(cache.parent.parent, root / "tmp")
                    Path(command[command.index("--output") + 1]).write_bytes(b"video")
                elif command[0] == "ffprobe":
                    return "22000\n"
                elif command[0] == "ffmpeg":
                    for number in range(1, 4):
                        Path(command[-1].replace("%d", str(number))).write_bytes(b"part")

            with patch.object(downloader, "run", side_effect=fake_run):
                manual.download_url("https://www.twitch.tv/videos/123", root, "720p60")
            expected = {f"S001E{number:02d}_2026-10-14_18-30Z_Test stream_123.mp4" for number in range(1, 4)}
            self.assertEqual({p.name for p in root.glob("*.mp4")}, expected)
            self.assertEqual(list((root / "tmp").iterdir()), [])
            channel = root / "VODs" / "test"
            channel.mkdir(parents=True)
            (channel / "Teil1_10.mp4").write_bytes(b"old")
            (channel / "Teil1_20.mp4").write_bytes(b"new")
            downloader.cleanup(channel, 1)
            self.assertEqual({p.name for p in root.glob("*.mp4")}, expected)
            with patch.object(downloader, "run", return_value=json.dumps(item)) as command:
                manual.download_url("https://www.twitch.tv/videos/123", root, "")
                self.assertEqual(command.call_count, 1)  # Metadata only; no duplicate download.

    def test_live_or_mismatched_metadata_does_not_start_download(self):
        for item in ({"id": "123", "status": "RECORDING"}, {"id": "456", "status": "RECORDED"}):
            with patch.object(downloader, "run", return_value=json.dumps(item)), patch.object(downloader, "download_video") as download:
                with self.assertRaises(ValueError):
                    manual.download_url("https://www.twitch.tv/videos/123", Path("/vods"), "")
                download.assert_not_called()

    def test_usage_and_failures_return_nonzero_without_prompts(self):
        with patch("sys.argv", ["tdl"]), self.assertLogs(manual.LOG, level="ERROR"):
            self.assertEqual(manual.main(), 1)
        with patch("sys.argv", ["tdl", "bad"]), self.assertLogs(manual.LOG, level="ERROR"):
            self.assertEqual(manual.main(), 1)


if __name__ == "__main__":
    unittest.main()
