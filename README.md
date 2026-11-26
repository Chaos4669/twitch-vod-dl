# Twitch VOD DL Docker Image

Automatically downloads Twitch VODs using [twitch-dl](https://github.com/ihabunek/twitch-dl) and splits long recordings using FFmpeg.

## How It Works

- The container stays running and starts its first check immediately on startup. Each cycle finishes all downloads, video splitting, and cleanup before starting a one-hour pause. The next check begins only after that pause ends.
- It fetches the latest `VOD_COUNT` archived broadcasts, newest first, and downloads any that are missing locally, processing the selected VODs oldest first.
- A VOD still being recorded is skipped. With `VOD_COUNT=5`, a recording among the latest five leaves four completed VODs to check. With `VOD_COUNT=1`, a live VOD leaves no download candidates until it finishes.
- Downloads are saved in `/vods/VODs/<channel>/` with filenames such as `S001E01_2026-10-14_18-30Z_Test stream_1234567890.mp4`. Each VOD gets one season, starting at `S001`, and its parts are numbered as episodes starting at `E01`. Season numbers use at least three digits and episode numbers at least two. Videos up to three hours have one episode. Longer videos are split into roughly three-hour episodes. All episodes of a broadcast count as one VOD.
- The timestamp is the start of the VOD recording (`recordedAt` from Twitch), normalized to UTC with minute precision. All episodes use the same timestamp. Titles are sanitized and shortened to 20 characters. The title is omitted if it is empty after sanitization. If the recording start time is missing or invalid, the downloader retries the VOD on later checks while it remains in the configured window.
- Season numbers are determined entirely from existing VOD filenames in each output directory: the highest `S...E...` season number plus one, or `S001` if none exist. No database or tracking file is created or read. Gaps below the highest season are not filled. Deleting the highest season allows its number to be reused; removing all seasons resets numbering to `S001`. All episodes of a new VOD share the same season number. Run one downloader per output directory so downloads do not assign seasons concurrently.
- The VOD ID at the end of each filename is used for duplicate detection and cleanup. Existing downloads in older formats (including `Teil` filenames) remain recognized without renaming or downloading them again; they do not reserve seasons. Existing season numbers stay fixed, so manually downloading an older VOD later gives it the next season rather than renumbering the library.
- A VOD is skipped when matching local MP4 files exist and all are nonempty. Their contents and expected part count are not verified. Download chunks, assembly, and splitting use a temporary folder under `/vods/tmp/`. Completed parts are moved into `/vods/VODs/<channel>/`, and the temporary folder is removed after success or failure.
- By default, cleanup keeps at most `VOD_COUNT` different saved VODs, including while the channel is live. Older VOD IDs are deleted together with all their parts; download modification times do not affect their order. Set `DELETE_OLD_VODS=false` to keep all saved VODs and disable retention cleanup. `VOD_COUNT` still limits the latest broadcasts checked each cycle; older broadcasts outside that window are not backfilled.
- Cleanup also runs when there are no new downloads. If a download or listing fails, cleanup is skipped for that cycle to preserve existing videos. The next cycle retries after the current cycle ends and the one-hour pause has elapsed.

For example, if a cycle starts at 12:00 and finishes downloading, splitting, and cleanup at 14:00, the next check starts at 15:00. Downloads can take longer than one hour and continue until completion; the one-hour pause is not a download timeout. Cycles run sequentially, so no additional check starts while a previous cycle is still running.

## Building the Image

Build the Alpine Linux runtime image for `linux/amd64` from the project directory.

```bash
docker build --pull --platform linux/amd64 -t twitch-vod-dl:latest .
```

## Running the Container

```bash
docker run -d --name twitch-vod-dl --restart unless-stopped \
  -e CHANNEL_NAME=Couriway \
  -e VOD_QUALITY=720p60 \
  -e VOD_COUNT=5 \
  -e DELETE_OLD_VODS=true \
  -v /path/on/host/vods:/vods \
  twitch-vod-dl:latest
```

Follow progress with `docker logs -f twitch-vod-dl`. Stop with `docker stop twitch-vod-dl`.

## Configuration

Configuration uses environment variables exclusively. Launch arguments are rejected.

| Environment variable | Default | Description |
| :--- | :--- | :--- |
| `CHANNEL_NAME` | `Couriway` | Twitch channel login. |
| `VOD_QUALITY` | Empty (`source`) | Desired quality passed to twitch-dl, e.g. `1080p60`, `720p60`, or `480p`. The requested quality must be available for the VOD. |
| `VOD_COUNT` | `5` | Positive integer: latest VODs to check and, when cleanup is enabled, maximum distinct VODs to retain. A recording VOD occupies one download slot and is skipped. |
| `DELETE_OLD_VODS` | `true` | Set to `false` to disable automatic deletion of older saved VODs. Accepts `true` or `false` (case-insensitive). Temporary files from downloads are still cleaned up. |

Changing environment variables requires recreating the container. Reducing `VOD_COUNT` removes older saved VODs on the next successful check only if `DELETE_OLD_VODS=true`. Re-enabling deletion also applies this limit on the next successful check.

## Persistence

Mount a writable host directory or Kubernetes PersistentVolume at `/vods` to keep downloads across container replacements. Videos are saved under `/vods/VODs/<channel>/`; temporary download files use `/vods/tmp/` on the same mount. Mount the outer host folder containing both `VODs` and `tmp`, rather than mounting `VODs` alone. The layout inside the container is:

```text
/vods/
├── VODs/
│   └── Couriway/
│       └── S001E01_2026-10-14_18-30Z_Test stream_1234567890.mp4
└── tmp/
```

Channel folder names and the `VODs` folder are case-sensitive inside the Linux container. Existing downloads must be under `/vods/VODs/<channel>/` to be recognized; files in the previous `/vods/<channel>/` location are not moved automatically.

For Jellyfin, point a **Shows** library at the `VODs` subdirectory of the mounted host folder, with each channel directory as a series folder. This keeps `/vods/tmp/` outside the library. The `S001E01`, `S001E02`, etc. prefixes identify the VOD's season and individual episodes. Files remain directly in the output directory. Jellyfin's [TV show documentation](https://jellyfin.org/docs/general/server/media/shows/) describes its recommended series/season folder organization.

## Manual Downloads

Use `tdl` to download an individual Twitch VOD:

```bash
docker exec -it twitch-vod-dl tdl "https://www.twitch.tv/videos/1234567890"
```

From a shell inside the container:

```sh
tdl "https://www.twitch.tv/videos/1234567890"
```

The command retrieves the VOD title and recording start time, uses the configured `VOD_QUALITY`, and applies the same filename format and approximately three-hour splitting as automatic downloads. If `VOD_QUALITY` is empty, it requests source quality.

Files are saved directly in `/vods`:

```text
/vods/S001E01_2026-10-14_18-30Z_Test stream_1234567890.mp4
```

Manual downloads are excluded from automatic cleanup and do not count toward `VOD_COUNT`. They remain on the mounted volume until manually deleted. VODs already present in `/vods` are skipped if all matching files are nonempty. Manual downloads use the highest season among VOD files directly in `/vods` plus one, independent of the per-channel automatic downloads.

Only completed VODs are supported. If a download fails, run the command again to retry.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use mocked Twitch/downloader commands and temporary directories; they do not download real VODs.

## License and use

The [MIT license](LICENSE) covers the source code and documentation of this project. Each dependency retains its own license.

[twitch-dl 3.3.2](https://pypi.org/project/twitch-dl/3.3.2/) is GPLv3-licensed. Alpine also ships GPL-licensed software such as [FFmpeg](https://pkgs.alpinelinux.org/package/v3.23/community/x86_64/ffmpeg) and [BusyBox](https://pkgs.alpinelinux.org/package/v3.23/main/x86_64/busybox). Release images built with the [release Dockerfile](tools/Dockerfile.release) include the corresponding sources and license notices as a `.tar.gz` archive with a SHA-256 checksum under `/usr/share/twitch-vod-dl/sources/`.

Download VODs only where you have permission under [Twitch's Terms of Service](https://www.twitch.tv/p/en/terms-of-service/) and the applicable content rights.
