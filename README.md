# Twitch VOD DL Docker Image

Automatically downloads Twitch VODs using [twitch-dl](https://github.com/ihabunek/twitch-dl) and splits long recordings using FFmpeg.

## How It Works

- The container stays running and starts its first check immediately on startup. Each cycle finishes all downloads, video splitting, and cleanup before starting a one-hour pause. The next check begins only after that pause ends.
- It fetches the latest `VOD_COUNT` archived broadcasts, newest first, and downloads any that are missing locally, processing the selected VODs oldest first.
- A VOD still being recorded is skipped. With `VOD_COUNT=5`, a recording among the latest five leaves four completed VODs to check. With `VOD_COUNT=1`, a live VOD leaves no download candidates until it finishes.
- Downloads are saved to `/vods/VODs/<channel>/` as `S<season>E<episode>_<YYYY-MM-DD_HH-MMZ>_<title>_<vod_id>.mp4`, for example `S001E01_2026-10-14_18-30Z_Test stream_1234567890.mp4`. Each VOD gets one season, starting at `S001`; its parts are episodes starting at `E01`. Season numbers use at least three digits and episodes at least two. Videos longer than three hours are split into approximately three-hour episodes; shorter videos have only `E01`. All episodes of a broadcast count as one VOD.
- The timestamp is the start of the VOD recording (`recordedAt` from Twitch), normalized to UTC with minute precision; `Z` means UTC. It is not the download or publication time. All episodes use the same stream start timestamp. Titles are sanitized and shortened to 20 characters. Missing or invalid recording start times cause that VOD to be retried on the next check.
- Season numbers are determined entirely from existing VOD filenames in each output directory: the highest `S...E...` season number plus one, or `S001` if none exist. No database or tracking file is created or read. Gaps below the highest season are not filled. Deleting the highest season allows its number to be reused; removing all seasons resets numbering to `S001`. All episodes of a new VOD share the same season number. Run one downloader per output directory so downloads do not assign seasons concurrently.
- The VOD ID at the end of each filename is used for duplicate detection and cleanup. Existing downloads in older formats (including `Teil` filenames) remain recognized without renaming or downloading them again; they do not reserve seasons. Existing season numbers stay fixed, so manually downloading an older VOD later gives it the next season rather than renumbering the library.
- A VOD is skipped when matching local MP4 files exist and all are nonempty. Their contents and expected part count are not verified. Download chunks, assembly, and splitting use a temporary folder under `/vods/tmp/`. Completed parts are moved into `/vods/VODs/<channel>/`, and the temporary folder is removed after success or failure.
- By default, cleanup keeps at most `VOD_COUNT` different saved VODs, including while the channel is live. Older VOD IDs are deleted together with all their parts; download modification times do not affect their order. Set `DELETE_OLD_VODS=false` to keep all saved VODs and disable retention cleanup. `VOD_COUNT` still limits the latest broadcasts checked each cycle; older broadcasts outside that window are not backfilled.
- Cleanup also runs when there are no new downloads. If a download or listing fails, cleanup is skipped for that cycle to preserve existing videos. The next cycle retries after the current cycle ends and the one-hour pause has elapsed.

For example, if a cycle starts at 12:00 and finishes downloading, splitting, and cleanup at 14:00, the next check starts at 15:00. Downloads can take longer than one hour and continue until completion; the one-hour pause is not a download timeout. Cycles run sequentially, so no additional check starts while a previous cycle is still running.

## Building the Image

Build the Alpine Linux image from the project directory.

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

Season numbering is recovered from the video filenames after a restart. Any `.vod-seasons.sqlite3` file left by an earlier version is ignored and can be deleted.

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

- [twitch-dl 3.3.2](https://pypi.org/project/twitch-dl/3.3.2/) is GPLv3-licensed. Alpine also ships GPL-licensed software such as [FFmpeg](https://pkgs.alpinelinux.org/package/v3.23/community/x86_64/ffmpeg) and [BusyBox](https://pkgs.alpinelinux.org/package/v3.23/main/x86_64/busybox). Anyone distributing a built image must satisfy the terms for the components in that image, including providing corresponding source where required for the exact versions shipped.

Download VODs only where you have permission under [Twitch's Terms of Service](https://www.twitch.tv/p/en/terms-of-service/) and the applicable content rights.

### Release source archive

Run these commands from the project checkout to build a release image containing the corresponding sources and license notices. The collector reads the installed Alpine and Python package metadata and the Rust dependency metadata embedded in the native libraries. It verifies the source downloads against their upstream checksums and records the runtime image digest in the inventory.

```bash
image=twitch-vod-dl:runtime
docker build --pull --platform linux/amd64 -t "$image" .
digest=$(docker image inspect --format '{{.Id}}' "$image")
mkdir -p release-sources
docker run --rm \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --mount "type=bind,source=$PWD/release-sources,target=/release" \
  --entrypoint python3 "$image" /workspace/tools/collect_release_sources.py \
  --image "$digest" --output /release

archive="twitch-vod-dl-sources-${digest#sha256:}-linux-amd64.tar.gz"
docker build --platform linux/amd64 -t twitch-vod-dl:latest \
  --build-arg "RUNTIME_IMAGE=$image" --build-arg "SOURCE_ARCHIVE=$archive" \
  -f - release-sources < tools/Dockerfile.release
```

Use a fresh output directory for a different runtime image digest. The final release includes the verified `.tar.gz` and its checksum at `/usr/share/twitch-vod-dl/sources/`. Use a stopped container to copy the files out of the image.

```bash
container=$(docker create twitch-vod-dl:latest)
mkdir -p image-sources
docker cp "$container:/usr/share/twitch-vod-dl/sources/." image-sources/
docker rm "$container"
```
