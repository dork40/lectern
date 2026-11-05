# Maintainer: Lectern contributors
# Builds from this source tree: run `makepkg -si` in the project directory.
# For an AUR upload, replace prepare() with a release tarball in source=().
pkgname=lectern
pkgver=0.8.0
pkgrel=1
pkgdesc="Audiobook player with sleep timer, automatic cover art and volume boost (GTK4/libadwaita)"
arch=('any')
url="https://aur.archlinux.org/packages/lectern"
license=('GPL-3.0-or-later')
depends=(
  'python'
  'python-gobject'
  'python-cairo'
  'python-mutagen'
  'python-cryptography'  # AirPlay pairing
  'gtk4'
  'libadwaita'
  'gdk-pixbuf2'
  'pango'
  'gstreamer'
  'gst-plugins-base'
  'gst-plugins-good'
  'gst-plugins-bad'
  'gst-plugins-ugly'
  'gst-libav'
  'swh-plugins'      # look-ahead limiter + compressor for the volume boost
  'python-pychromecast'  # casting to Chromecast / Google TV / Android TV
  'python-zeroconf'      # finding Roku TVs and cast devices on the network
)
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
optdepends=('playerctl: control playback from the command line'
            'owntone-server: AirPlay TVs and speakers, e.g. Roku TVs (AUR)'
            'avahi: needed by OwnTone to find AirPlay devices')
source=()
sha256sums=()

prepare() {
  rm -rf "$srcdir/$pkgname"
  mkdir -p "$srcdir/$pkgname"
  cp -r "$startdir"/{lectern,data,tests,pyproject.toml,README.md,LICENSE} "$srcdir/$pkgname/"
  find "$srcdir/$pkgname" -name __pycache__ -prune -exec rm -rf {} +
}

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

check() {
  cd "$srcdir/$pkgname"
  python -m pytest -q
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/app.lectern.Lectern.desktop -t "$pkgdir/usr/share/applications/"
  install -Dm644 data/app.lectern.Lectern.metainfo.xml -t "$pkgdir/usr/share/metainfo/"
  install -Dm644 data/icons/hicolor/scalable/apps/app.lectern.Lectern.svg \
    -t "$pkgdir/usr/share/icons/hicolor/scalable/apps/"
  install -Dm644 data/icons/hicolor/symbolic/apps/app.lectern.Lectern-symbolic.svg \
    -t "$pkgdir/usr/share/icons/hicolor/symbolic/apps/"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
