import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import downloader as dl


def video(vod_id, status="RECORDED"):
    return {"id": str(vod_id), "title": "Test stream", "status": status,
            "recordedAt": "2026-10-14T18:30:00Z"}


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.directory = Path(self.work.name) / "VODs" / "channel"
        self.directory.mkdir(parents=True)

    def save(self, name, content=b"video"):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def test_environment_configuration_and_no_launch_arguments(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["downloader.py"]):
            self.assertEqual(dl.configuration(), ("Couriway", "", 5, True))
        with patch.dict(os.environ, {"CHANNEL_NAME": "test", "VOD_QUALITY": "720p60", "VOD_COUNT": "3", "DELETE_OLD_VODS": "false"}, clear=True), patch("sys.argv", ["downloader.py"]):
            self.assertEqual(dl.configuration(), ("test", "720p60", 3, False))
            for invalid in ("0", "-1", "bad", "1.5", ""):
                with patch.dict(os.environ, {"VOD_COUNT": invalid}), self.assertRaises(ValueError):
                    dl.configuration()
        with patch("sys.argv", ["downloader.py", "channel"]), self.assertRaises(ValueError):
            dl.configuration()

    def test_main_uses_nested_streamer_folder_and_separate_temporary_folder(self):
        self.save("S005E01_2026-10-14_18-30Z_Test stream_20.mp4")

        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                self.assertEqual(command[2], "30")
                self.assertEqual(command[-2:], ["--quality", "source"])
                self.assertIn("--skip-live", command)
                cache = Path(command[command.index("--cache-dir") + 1])
                self.assertEqual(cache.parent.parent, Path(self.work.name) / "tmp")
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "100.0\n"

        def fake_serve(channel, quality, count, directory, stop, **kwargs):
            self.assertEqual(directory, self.directory)
            dl.check_once(channel, quality, count, directory, **kwargs)

        with patch.object(dl, "STORAGE_ROOT", Path(self.work.name)), patch.object(dl, "configuration", return_value=("channel", "", 2, False)), patch.object(dl.logging, "basicConfig"), patch.object(dl.signal, "signal"), patch.object(dl, "serve", side_effect=fake_serve), patch.object(dl, "fetch_videos", return_value=[video(30), video(20)]), patch.object(dl, "run", side_effect=fake_run) as command:
            self.assertEqual(dl.main(), 0)
        self.assertEqual(command.call_count, 2)  # Only the new VOD is downloaded.
        self.assertTrue((self.directory / "S006E01_2026-10-14_18-30Z_Test stream_30.mp4").exists())
        self.assertEqual(list((Path(self.work.name) / "tmp").iterdir()), [])
        self.assertFalse((self.directory.parent / "tmp").exists())
        self.assertFalse((self.directory / "tmp").exists())

    def test_deletion_setting_accepts_booleans_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["downloader.py"]):
            for value, expected in (("true", True), ("FALSE", False), (" True ", True)):
                with patch.dict(os.environ, {"DELETE_OLD_VODS": value}):
                    self.assertIs(dl.configuration()[3], expected)
            for value in ("", "0", "1", "yes", "bad"):
                with patch.dict(os.environ, {"DELETE_OLD_VODS": value}), self.assertRaises(ValueError):
                    dl.configuration()

    def test_latest_window_excludes_live_without_backfilling(self):
        payload = {"videos": [video(30, "RECORDING"), video(20), video(10)]}
        with patch.object(dl, "run", return_value=json.dumps(payload)) as command:
            self.assertEqual(dl.fetch_videos("test", 3), [video(20), video(10)])
            self.assertEqual(dl.fetch_videos("test", 1), [])
            self.assertIn("--json", command.call_args.args[0])
        payload["videos"][0]["status"] = "RECORDED"
        with patch.object(dl, "run", return_value=json.dumps(payload)):
            self.assertEqual(len(dl.fetch_videos("test", 3)), 3)

    def test_cleanup_groups_parts_and_uses_exact_ids_not_modification_time(self):
        self.save("Title123_Teil1_20.mp4")
        self.save("Title123_Teil2_20.mp4")
        self.save("Teil1_120.mp4")
        self.save("10.mp4")
        unrelated = self.save("notes.txt")
        os.utime(self.directory / "10.mp4", (2000000000, 2000000000))
        dl.cleanup(self.directory, 2)
        self.assertEqual(set(dl.saved_vods(self.directory)), {"20", "120"})
        dl.cleanup(self.directory, 1)
        self.assertEqual(set(dl.saved_vods(self.directory)), {"120"})
        self.assertTrue(unrelated.exists())

    def test_existing_vod_skipped_and_similar_id_does_not_skip_download(self):
        self.save("Teil1_120.mp4")
        with patch.object(dl, "run") as command:
            dl.download_video(video(120), self.directory, "")
            command.assert_not_called()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:2] == ["twitch-dl", "download"]:
                cache = Path(command[command.index("--cache-dir") + 1])
                self.assertTrue(cache.is_dir())
                self.assertEqual(cache.parent.parent, self.directory.parent.parent / "tmp")
                (cache / "chunk.ts").write_bytes(b"chunk")
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "100.0\n"

        with patch.object(dl, "run", side_effect=fake_run):
            dl.download_video(video(20), self.directory, "720p60")
        self.assertEqual(set(dl.saved_vods(self.directory)), {"20", "120"})
        self.assertEqual(calls[0][-2:], ["--quality", "720p60"])
        self.assertTrue((self.directory / "S001E01_2026-10-14_18-30Z_Test stream_20.mp4").exists())
        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.assertEqual(list((self.directory.parent.parent / "tmp").iterdir()), [])

    def test_recording_time_is_used_and_normalized_to_utc(self):
        item = video(20)
        item.update(recordedAt="2026-10-14T20:30:45+02:00",
                    createdAt="2026-10-15T10:00:00Z", publishedAt="2026-10-16T10:00:00Z")
        self.assertEqual(dl.stream_timestamp(item), "2026-10-14_18-30Z")
        for invalid in (None, "bad", "2026-10-14T18:30:00"):
            item["recordedAt"] = invalid
            with patch.object(dl, "run") as command, self.assertRaises(ValueError):
                dl.download_video(item, self.directory, "")
            command.assert_not_called()

    def test_timestamp_and_id_support_skipping_and_cleanup_with_legacy_files(self):
        name = "2026-10-14_18-30Z_Teil1_Test stream_20.mp4"
        self.save(name)
        self.save("Test stream_Teil2_2026-10-14_18-30-00Z_20.mp4")
        self.save("Teil1_10.mp4")
        with patch.object(dl, "run") as command:
            dl.download_video(video(20), self.directory, "")
            command.assert_not_called()
        dl.cleanup(self.directory, 1)
        self.assertEqual(set(dl.saved_vods(self.directory)), {"20"})
        self.assertTrue((self.directory / name).exists())
        self.save("Teil1_30.mp4")
        dl.cleanup(self.directory, 1)
        self.assertFalse((self.directory / name).exists())

    def test_long_vod_splits_and_all_parts_are_saved(self):
        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "22000.0\n"
            elif command[0] == "ffmpeg":
                for number in range(1, 4):
                    Path(command[-1].replace("%d", str(number))).write_bytes(b"part")

        with patch.object(dl, "run", side_effect=fake_run):
            dl.download_video(video(20), self.directory, "")
        self.assertEqual(len(dl.saved_vods(self.directory)["20"]), 3)
        self.assertEqual({path.name for path in dl.saved_vods(self.directory)["20"]},
                         {f"S001E{number:02d}_2026-10-14_18-30Z_Test stream_20.mp4"
                          for number in range(1, 4)})
        self.assertFalse((self.directory / "original.mp4").exists())
        with patch.object(dl, "run") as command:
            dl.download_video(video(20), self.directory, "")
            command.assert_not_called()

    def test_same_timestamp_and_title_keep_different_vods_separate(self):
        name = "2026-10-14_18-30Z_Teil1_Test stream_30.mp4"
        other = self.save(name, b"other VOD")

        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                Path(command[command.index("--output") + 1]).write_bytes(b"new VOD")
            elif command[0] == "ffprobe":
                return "100.0\n"

        with patch.object(dl, "run", side_effect=fake_run):
            dl.download_video(video(20), self.directory, "")
        self.assertEqual(other.read_bytes(), b"other VOD")
        self.assertEqual(set(dl.saved_vods(self.directory)), {"20", "30"})
        self.assertEqual((self.directory / "S001E01_2026-10-14_18-30Z_Test stream_20.mp4").read_bytes(), b"new VOD")
        self.assertEqual(list(self.directory.glob("*.json")), [])

    def test_failed_download_leaves_no_partial_file_and_retries(self):
        attempts = 0

        def fake_run(command, **kwargs):
            nonlocal attempts
            if command[:2] == ["twitch-dl", "download"]:
                attempts += 1
                cache = Path(command[command.index("--cache-dir") + 1])
                (cache / "chunk.ts").write_bytes(b"partial")
                Path(command[command.index("--output") + 1]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(1, command)

        with patch.object(dl, "run", side_effect=fake_run):
            for _ in range(2):
                with self.assertRaises(subprocess.CalledProcessError):
                    dl.download_video(video(20), self.directory, "")
        self.assertEqual(attempts, 2)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(list((self.directory.parent.parent / "tmp").iterdir()), [])

    def test_download_exit_without_output_preserves_older_vod(self):
        old_vod = self.save("Teil1_10.mp4")
        with patch.object(dl, "fetch_videos", return_value=[video(20)]), patch.object(dl, "run") as command, self.assertLogs(dl.LOG, level="ERROR"):
            dl.check_once("test", "", 1, self.directory)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0][:3], ["twitch-dl", "download", "20"])
        self.assertTrue(old_vod.exists())
        self.assertEqual(set(dl.saved_vods(self.directory)), {"10"})
        self.assertEqual(list((self.directory.parent.parent / "tmp").iterdir()), [])

    def test_failure_continues_other_downloads_and_preserves_old_vods(self):
        self.save("Teil1_10.mp4")
        self.save("Teil1_11.mp4")
        with patch.object(dl, "fetch_videos", return_value=[video(30), video(20)]), patch.object(dl, "download_video", side_effect=[OSError("failed"), None]) as download, patch.object(dl, "cleanup") as cleanup, self.assertLogs(dl.LOG, level="ERROR"):
            dl.check_once("test", "", 2, self.directory)
        self.assertEqual(download.call_count, 2)
        cleanup.assert_not_called()

    def test_interrupted_publication_removes_already_published_parts(self):
        original_replace = Path.replace

        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "22000.0\n"
            elif command[0] == "ffmpeg":
                for number in range(1, 4):
                    Path(command[-1].replace("%d", str(number))).write_bytes(b"part")

        moved = 0

        def interrupt_replace(path, target):
            nonlocal moved
            moved += 1
            if moved == 2:
                raise SystemExit(0)
            return original_replace(path, target)

        with patch.object(dl, "run", side_effect=fake_run), patch.object(Path, "replace", interrupt_replace):
            with self.assertRaises(SystemExit):
                dl.download_video(video(20), self.directory, "")
        self.assertEqual(dl.saved_vods(self.directory), {})
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(dl.next_season(self.directory), 1)
        with patch.object(dl, "run", side_effect=fake_run):
            dl.download_video(video(20), self.directory, "")
        self.assertEqual({path.name for path in self.directory.iterdir()},
                         {f"S001E{number:02d}_2026-10-14_18-30Z_Test stream_20.mp4"
                          for number in range(1, 4)})

    def test_seasons_increase_oldest_first_and_survive_cleanup(self):
        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "100.0\n"

        with patch.object(dl, "run", side_effect=fake_run):
            with patch.object(dl, "fetch_videos", return_value=[video(30), video(20)]):
                dl.check_once("test", "", 2, self.directory)
            self.assertTrue((self.directory / "S001E01_2026-10-14_18-30Z_Test stream_20.mp4").exists())
            self.assertTrue((self.directory / "S002E01_2026-10-14_18-30Z_Test stream_30.mp4").exists())
            dl.cleanup(self.directory, 1)
            dl.download_video(video(40), self.directory, "")
        self.assertEqual(set(dl.saved_vods(self.directory)), {"30", "40"})
        self.assertTrue((self.directory / "S003E01_2026-10-14_18-30Z_Test stream_40.mp4").exists())

    def test_next_season_reads_numeric_maximum_without_filling_gaps(self):
        self.save("S003E01_2026-10-14_18-30Z_Title_20.mp4")
        self.save("S003E02_2026-10-14_18-30Z_Title_20.mp4")
        self.save("s010e01_2026-10-14_18-30Z_Title_30.MP4")
        self.assertEqual(dl.next_season(self.directory), 11)

    def test_next_season_reuses_deleted_highest_and_resets_when_empty(self):
        lower = self.save("S003E01_Title_20.mp4")
        highest = self.save("S005E01_Title_30.mp4")
        self.assertEqual(dl.next_season(self.directory), 6)
        highest.unlink()
        self.assertEqual(dl.next_season(self.directory), 4)
        lower.unlink()
        self.assertEqual(dl.next_season(self.directory), 1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_next_season_ignores_unrelated_files_and_other_streamers(self):
        self.save("2026-10-14_18-30Z_Teil1_Title_20.mp4")
        self.save("S999E01_notes.txt")
        self.save("S999E01_Title.mp4")
        (self.directory / "S999E01_Title_40.mp4").mkdir()
        other = self.directory.parent / "other-streamer"
        other.mkdir()
        (other / "S999E01_Title_50.mp4").write_bytes(b"video")
        self.assertEqual(dl.next_season(self.directory), 1)

    def test_episode_filename_without_title_and_large_numbers(self):
        self.save("S999E01_Title_10.mp4")

        def fake_run(command, **kwargs):
            if command[:2] == ["twitch-dl", "download"]:
                Path(command[command.index("--output") + 1]).write_bytes(b"complete")
            elif command[0] == "ffprobe":
                return "120000.0\n"
            elif command[0] == "ffmpeg":
                for number in range(1, 13):
                    Path(command[-1].replace("%d", str(number))).write_bytes(str(number).encode())

        item = video(20)
        item["title"] = ""
        with patch.object(dl, "run", side_effect=fake_run):
            dl.download_video(item, self.directory, "")
        for number in range(1, 13):
            path = self.directory / f"S1000E{number:02d}_2026-10-14_18-30Z_20.mp4"
            self.assertEqual(path.read_bytes(), str(number).encode())

    def test_cleanup_runs_even_when_vods_already_exist_or_channel_is_empty(self):
        for videos in ([video(20)], []):
            with patch.object(dl, "fetch_videos", return_value=videos), patch.object(dl, "download_video"), patch.object(dl, "cleanup") as cleanup:
                dl.check_once("test", "", 2, self.directory)
                cleanup.assert_called_once_with(self.directory, 2)

    def test_disabled_deletion_preserves_old_vods_and_still_checks_downloads(self):
        old = self.save("Teil1_10.mp4")
        newer = self.save("2026-10-14_18-30Z_Teil1_Test stream_20.mp4")
        for videos in ([video(30)], []):
            with patch.object(dl, "fetch_videos", return_value=videos) as fetch, patch.object(dl, "download_video") as download, patch.object(dl, "cleanup") as cleanup:
                dl.check_once("test", "720p60", 1, self.directory, delete_old_vods=False)
                fetch.assert_called_once_with("test", 1)
                if videos:
                    download.assert_called_once_with(video(30), self.directory, "720p60")
                else:
                    download.assert_not_called()
                cleanup.assert_not_called()
            self.assertEqual(old.read_bytes(), b"video")
            self.assertEqual(newer.read_bytes(), b"video")

    def test_service_passes_disabled_deletion_to_each_check(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(dl, "check_once") as check:
            dl.serve("test", "", 2, self.directory, stop, delete_old_vods=False)
        check.assert_called_once_with("test", "", 2, self.directory, delete_old_vods=False)

    def test_immediate_check_hourly_wait_and_recovery_after_listing_failure(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        events = []

        def check(*args, **kwargs):
            events.append("check")
            if len(events) == 1:
                raise ValueError("invalid JSON")

        stop.wait.side_effect = lambda seconds: events.append(seconds)
        with patch.object(dl, "check_once", side_effect=check), self.assertLogs(dl.LOG, level="ERROR"):
            dl.serve("test", "", 2, self.directory, stop)
        self.assertEqual(events, ["check", 3600, "check", 3600])


if __name__ == "__main__":
    unittest.main()
