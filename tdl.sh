#!/bin/sh
set -eu

exec python3 /usr/local/bin/manual_download.py "$@"
