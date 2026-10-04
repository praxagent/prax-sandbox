#!/bin/sh
# record-apt-packages — dpkg hook (DPkg::Post-Invoke, /etc/apt/apt.conf.d/
# 99prax-record). After every apt run, records the packages installed since
# the image was built, by anyone (Prax, the TeamWork terminal, the desktop),
# in /workspace/.sandbox/installed-apt.txt.
#
# That file is on the workspace mount, so it survives the container being
# recreated, which is exactly when those packages are lost. It is the list to
# copy from into sandbox/local-packages.txt. The list only grows (a later
# container does not have the packages, and must not erase the record),
# minus anything the image now carries.
#
# Never fails the apt run: every error exits 0.
#   BASELINE   packages the image was built with (written at the end of the build)
#   OUT        output file
#   APT_MARK   apt-mark binary (tests point it at a fake)
baseline="${BASELINE:-/etc/prax-sandbox/baseline-packages.txt}"
out="${OUT:-/workspace/.sandbox/installed-apt.txt}"
apt_mark="${APT_MARK:-apt-mark}"
export LC_ALL=C

[ -f "$baseline" ] || exit 0          # during the image build: nothing to record yet
mkdir -p "$(dirname "$out")" 2>/dev/null || exit 0
tmp="$(mktemp)" || exit 0

{
  [ -f "$out" ] && grep -v '^#' "$out"
  "$apt_mark" showmanual 2>/dev/null | sort -u | comm -13 "$baseline" -
} | grep -v '^[[:space:]]*$' | sort -u | comm -23 - "$baseline" > "$tmp" 2>/dev/null

{
  echo "# apt packages installed in the sandbox since its image was built."
  echo "# Kept when the container is recreated (the packages themselves are not)."
  echo "# To keep one for good: add it to prax-sandbox/sandbox/local-packages.txt and rebuild."
  cat "$tmp"
} > "$out" 2>/dev/null
rm -f "$tmp"
exit 0
