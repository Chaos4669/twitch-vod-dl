#!/bin/sh
set -eu

# Configuration comes exclusively from environment variables.
exec python3 /usr/local/bin/downloader.py "$@"
