#!/usr/bin/env bash
# Generate a small synthetic audiobook library for development and testing.
# Usage: scripts/make-sample-library.sh <dest-dir>
set -euo pipefail
dest=${1:?usage: $0 <dest-dir>}
mkdir -p "$dest"
tone() { # tone <freq> <seconds> <out> [extra ffmpeg args...]
  local f=$1 s=$2 out=$3; shift 3
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$f:duration=$s" -ac 2 "$@" "$out"
}

# 1. Single m4b with chapter markers and tags
meta=$(mktemp)
cat > "$meta" <<META
;FFMETADATA1
title=The Quiet Orbit
artist=Mara Ellison
album=The Quiet Orbit
composer=Sam Reyes
genre=Audiobook
date=2021
[CHAPTER]
TIMEBASE=1/1000
START=0
END=40000
title=Opening Credits
[CHAPTER]
TIMEBASE=1/1000
START=40000
END=100000
title=Chapter One: Drift
[CHAPTER]
TIMEBASE=1/1000
START=100000
END=150000
title=Chapter Two: Signal
META
mkdir -p "$dest/Mara Ellison"
tone 330 150 "$dest/tmp.m4a" -c:a aac -b:a 64k
# synthetic cover art, embedded into the m4b
ffmpeg -loglevel error -y -f lavfi -i "mandelbrot=s=800x800:end_pts=1:inner=convergence" -frames:v 1 "$dest/cover.jpg"
ffmpeg -loglevel error -y -i "$dest/tmp.m4a" -i "$meta" -i "$dest/cover.jpg" -map 0:a -map 2:v -map_metadata 1 -map_chapters 1 \
  -c copy -disposition:v:0 attached_pic "$dest/Mara Ellison/The Quiet Orbit.m4b"
rm -f "$dest/tmp.m4a" "$meta" "$dest/cover.jpg"

# 2. Folder of mp3 files (Author/Series/Book layout), no album tags
book="$dest/Jonah Pike/Harbor Lights/Book 2 - The Lantern Keeper"
mkdir -p "$book"
for i in 1 2 3 10; do
  tone $((200 + i * 40)) 25 "$book/Part $i.mp3" -c:a libmp3lame -b:a 64k
done
# a folder.jpg next to the files
ffmpeg -loglevel error -y -f lavfi -i "mandelbrot=s=600x600:end_pts=1:start_x=-0.7436:start_y=0.1318:start_scale=0.004:outer=normalized_iteration_count" -frames:v 1 "$book/folder.jpg"

# 3. mp3 with ID3 chapters (CHAP frames)
meta=$(mktemp)
cat > "$meta" <<META
;FFMETADATA1
title=Short Stories at Night
artist=Ines Calloway
album=Short Stories at Night
[CHAPTER]
TIMEBASE=1/1000
START=0
END=30000
title=The Lighthouse
[CHAPTER]
TIMEBASE=1/1000
START=30000
END=60000
title=The Ferry
META
tone 440 60 "$dest/tmp.mp3" -c:a libmp3lame -b:a 64k
ffmpeg -loglevel error -y -i "$dest/tmp.mp3" -i "$meta" -map_metadata 1 -map_chapters 1 -c copy -id3v2_version 3 "$dest/Ines Calloway - Short Stories at Night.mp3"
rm -f "$dest/tmp.mp3" "$meta"

# 4. Opus and FLAC single files, untagged: metadata comes from the filename
tone 520 45 "$dest/Theo Marsh - A Field Guide to Clouds.opus" -c:a libopus -b:a 48k
tone 600 30 "$dest/Penny Arden - Small Hours.flac" -c:a flac
echo "Sample library written to $dest"
