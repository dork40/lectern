#!/usr/bin/env bash
# One-step install for Arch Linux: dependencies, build, tests, install.
#   ./install.sh            install (or update) Lectern
#   ./install.sh --run      ...and start it afterwards
#   ./install.sh --remove   uninstall (your library database and covers are kept)
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

if ! command -v pacman >/dev/null || ! command -v makepkg >/dev/null; then
  echo "This installer needs Arch Linux (pacman + makepkg)." >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user; it asks for sudo when needed." >&2
  exit 1
fi

if [ "${1:-}" = "--remove" ]; then
  sudo pacman -R lectern
  echo "Removed. Your library is still in ~/.local/share/lectern (delete it to start fresh)."
  exit 0
fi

# makepkg needs base-devel (fakeroot, etc.)
if ! pacman -Qq base-devel >/dev/null 2>&1; then
  echo ":: Installing base-devel (needed by makepkg)…"
  sudo pacman -S --needed base-devel
fi

echo ":: Building and installing Lectern (installs missing dependencies, runs the tests)…"
makepkg -si --needed --cleanbuild

# A running Lectern still has the old code loaded: quit it cleanly (it saves your
# position on quit) and start the new version.
was_running=false
if pgrep -f "/usr/bin/lectern" >/dev/null; then
  was_running=true
  gdbus call --session -d org.mpris.MediaPlayer2.lectern -o /org/mpris/MediaPlayer2 \
    -m org.mpris.MediaPlayer2.Quit >/dev/null 2>&1 || true
  for _ in $(seq 20); do pgrep -f "/usr/bin/lectern" >/dev/null || break; sleep 0.25; done
fi

# Chromecast: the TV/speaker streams the audio from this computer on TCP port 8765.
# Offer to open it to the local network only, if a firewall would block it.
lan=$(ip -o -f inet addr show scope global 2>/dev/null | awk 'NR==1{print $4}')
subnet=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$lan" 2>/dev/null || true)
if [ -n "$subnet" ] && command -v ufw >/dev/null && grep -q '^ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null; then
  if ! sudo ufw status | grep -q '8765/tcp'; then
    read -r -p ":: Allow Chromecast devices on $subnet to stream from Lectern (opens TCP port 8765 to your local network only)? [Y/n] " answer
    if [[ ! "$answer" =~ ^[Nn] ]]; then
      sudo ufw allow from "$subnet" to any port 8765 proto tcp comment 'Lectern Chromecast'
    fi
  fi
elif [ -n "$subnet" ] && command -v firewall-cmd >/dev/null && systemctl is-active -q firewalld; then
  read -r -p ":: Open TCP port 8765 in firewalld for Chromecast streaming? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn] ]]; then
    sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$subnet port port=8765 protocol=tcp accept"
    sudo firewall-cmd --reload
  fi
fi

# AirPlay TVs/speakers (Roku TVs, HomePods, Samsung/LG TVs…) go through OwnTone (AUR).
if ! command -v owntone >/dev/null && [ ! -x /usr/sbin/owntone ]; then
  read -r -p ":: Install OwnTone from the AUR for AirPlay TVs and speakers (e.g. Roku TVs)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn] ]]; then
    # the AUR build uses libraries it doesn't list as dependencies
    sudo pacman -S --needed --asdeps libwebsockets protobuf-c json-c libplist libsodium libgcrypt libunistring \
      curl gnutls libxml2 gperf
    if command -v yay >/dev/null; then yay -S --needed owntone-server
    elif command -v paru >/dev/null; then paru -S --needed owntone-server
    else echo "   No AUR helper found: build https://aur.archlinux.org/packages/owntone-server yourself."
    fi
  fi
fi
# Lectern needs two OwnTone fixes (no crash on cover art, 48 kHz for Roku TVs): rebuild it with them.
if pacman -Q owntone-server >/dev/null 2>&1 && [ "$(pacman -Q owntone-server | cut -d' ' -f2)" != "29.3-101" ]; then
  read -r -p ":: Rebuild OwnTone with Lectern's AirPlay fixes (takes a few minutes)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn] ]]; then
    owntone_build=$(mktemp -d)
    cp packaging/owntone/* "$owntone_build/"
    for cached in ~/.cache/yay/owntone-server/29.3.tar.gz ~/.cache/paru/clone/owntone-server/29.3.tar.gz; do
      [ -f "$cached" ] && cp "$cached" "$owntone_build/" && break
    done
    (cd "$owntone_build" && makepkg -si --noconfirm --needed) || echo "   OwnTone rebuild failed; AirPlay covers stay off."
    rm -rf "$owntone_build"
  fi
fi
if command -v owntone >/dev/null || [ -x /usr/sbin/owntone ]; then
  # OwnTone finds AirPlay devices through Avahi
  systemctl is-active -q avahi-daemon || sudo systemctl enable --now avahi-daemon
  # AirPlay 2 TVs keep time with PTP (UDP 319/320). Without it OwnTone falls back to NTP timing,
  # which Roku TVs play too fast and choppy. Let OwnTone bind those ports as a normal user:
  owntone_bin=$(command -v owntone || echo /usr/sbin/owntone)
  getcap "$owntone_bin" 2>/dev/null | grep -q cap_net_bind_service || sudo setcap cap_net_bind_service=+ep "$owntone_bin"
  # AirPlay devices send timing packets back to this computer on UDP 6001-6002 and 319-320
  if [ -n "$subnet" ] && command -v ufw >/dev/null && grep -q '^ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null \
     && ! grep -qE "6001:6002 .*${subnet%/*}" /etc/ufw/user.rules 2>/dev/null; then
    read -r -p ":: Allow AirPlay devices on $subnet to reach Lectern (UDP 6001-6002, local network only)? [Y/n] " answer
    if [[ ! "$answer" =~ ^[Nn] ]]; then
      sudo ufw allow from "$subnet" to any port 6001:6002 proto udp comment 'Lectern AirPlay'
    fi
  fi
  if [ -n "$subnet" ] && command -v ufw >/dev/null && grep -q '^ENABLED=yes' /etc/ufw/ufw.conf 2>/dev/null \
     && ! grep -qE "319:320 .*${subnet%/*}" /etc/ufw/user.rules 2>/dev/null; then
    read -r -p ":: Allow AirPlay clock sync (PTP, UDP 319-320) from $subnet? [Y/n] " answer
    if [[ ! "$answer" =~ ^[Nn] ]]; then
      sudo ufw allow from "$subnet" to any port 319:320 proto udp comment 'Lectern AirPlay PTP'
    fi
  fi
fi

echo ":: Cleaning up build files…"
chmod -R u+rwx src pkg 2>/dev/null; rm -rf src pkg ./*.pkg.tar.zst

echo
echo "Lectern is installed. Open it from your app launcher or run: lectern"
if [ "${1:-}" = "--run" ] || $was_running; then
  setsid -f lectern >/dev/null 2>&1
fi
