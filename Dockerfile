# syntax=docker/dockerfile:1
FROM alpine:3.23 AS build

ARG TARGETARCH
ARG TWITCH_DL_VERSION=3.3.2

# Prepare Python dependencies separately; pip stays out of the runtime image.
RUN test "$TARGETARCH" = "amd64" \
 && apk add --no-cache ca-certificates python3 py3-pip \
 && python3 -m venv --without-pip /opt/venv \
 && python3 -m pip --python /opt/venv install --no-cache-dir "twitch-dl==${TWITCH_DL_VERSION}"

FROM alpine:3.23 AS runtime
ENV APP_VERSION=1.5.0 \
    XDG_CACHE_HOME=/tmp

RUN apk add --no-cache \
    ca-certificates \
    ffmpeg \
    python3 \
    tzdata \
 && mkdir -p /vods \
 && chmod 0777 /vods

COPY LICENSE /usr/share/licenses/twitch-vod-dl/LICENSE
COPY --from=build /opt/venv /opt/venv
COPY --chmod=755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY downloader.py /usr/local/bin/downloader.py
COPY manual_download.py /usr/local/bin/manual_download.py
COPY --chmod=755 tdl.sh /usr/local/bin/tdl

RUN ln -s /opt/venv/bin/twitch-dl /usr/local/bin/twitch-dl \
 && XDG_CACHE_HOME=/tmp/build-cache twitch-dl --version \
 && rm -r /tmp/build-cache \
 && ffmpeg -version \
 && ffprobe -version

VOLUME ["/vods"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
