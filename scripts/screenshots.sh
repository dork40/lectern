#!/usr/bin/env bash
# Render README screenshots headlessly (GTK Broadway backend) from a synthetic
# library in a throwaway XDG sandbox. Usage: scripts/screenshots.sh [out-dir]
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(realpath -m "${1:-docs/screenshots}")
work=$(mktemp -d)
trap 'kill $broadway 2>/dev/null; rm -rf "$work"' EXIT
mkdir -p "$out"

export XDG_DATA_HOME=$work/data XDG_CACHE_HOME=$work/cache XDG_STATE_HOME=$work/state
export XDG_DATA_DIRS=$HOME/.local/share:/usr/local/share:/usr/share   # keep the user's icon theme
export GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 LECTERN_AUDIO_SINK="fakesink"
PY=${PYTHON:-python3}
[ -x .venv/bin/python ] && PY=.venv/bin/python

gtk4-broadwayd :9 >/dev/null 2>&1 & broadway=$!
scripts/make-sample-library.sh "$work/books" >/dev/null

PYTHONPATH=. "$PY" - "$work/books" <<'PYEOF'
import sys, time
from gi.repository import GLib
from lectern.const import DB_PATH, ensure_dirs
from lectern.db import Database
from lectern.scanner import Scanner
ensure_dirs()
db = Database(DB_PATH)
db.add_folder(sys.argv[1])
db.set_setting("cover_auto_fetch", False)
db.set_setting("window_size", [1100, 760])
loop = GLib.MainLoop()
sc = Scanner(db); sc.connect("finished", lambda *a: loop.quit()); sc.scan(); loop.run()
ids = {b.title: b.id for b in db.all_books()}
db.save_progress(ids["The Lantern Keeper"], 30.0); time.sleep(0.01)
db.save_progress(ids["The Quiet Orbit"], 55.0)
db.save_progress(ids["Small Hours"], 30.0, finished=True, touch=False)
db.set_setting("last_book", ids["The Quiet Orbit"])
PYEOF

shot() {  # shot <view> <file>
  LECTERN_VIEW=$1 LECTERN_SCREENSHOT="$out/$2" LECTERN_DELAY=2500 timeout 60 "$PY" -m lectern 2>/dev/null || true
  echo "  $out/$2"
}
echo "Writing screenshots:"
shot library library.png
shot nowplaying now-playing.png
shot book:Quiet book.png
shot sleep sleep-timer.png
