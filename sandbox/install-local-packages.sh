#!/usr/bin/env bash
# install-local-packages — install the apt packages listed in
# sandbox/local-packages.txt while the image is built, so they survive every
# container restart and recreation.
#
# The list is hand-edited and outlives the Debian release it was written for
# (packages get renamed), so a bad entry is SKIPPED AND REPORTED, never fatal:
# one typo must not block every later rebuild of the sandbox, security updates
# included. LOCAL_PACKAGES_STRICT=1 makes any skipped entry fail the build.
#
# What was skipped, and why, is printed here, written to
# /etc/prax-sandbox/local-packages.report (one "installed <name>" or
# "skipped <name> <reason>" per line), printed again at every container start,
# and by scripts/ensure-image.sh after a build.
#
# Usage: install-local-packages <list-file>
#   APT_GET   apt-get binary (tests point it at a fake)
#   REPORT    report path (default /etc/prax-sandbox/local-packages.report)
set -u

list="${1:-}"
apt_get="${APT_GET:-apt-get}"
report="${REPORT:-/etc/prax-sandbox/local-packages.report}"
strict="${LOCAL_PACKAGES_STRICT:-0}"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

mkdir -p "$(dirname "$report")"
: > "$report"

if [[ -z "$list" || ! -f "$list" ]]; then
  echo "==> local packages: none (no sandbox/local-packages.txt)"
  exit 0
fi

# Debian package names: lowercase letters, digits, + - . ; at least two
# characters, starting with a letter or digit. Optional :arch and =version.
name_re='^[a-z0-9][a-z0-9+.-]+(:[a-z0-9]+)?(=[A-Za-z0-9.+:~_-]+)?$'

declare -a valid=() installed=() skipped=()
declare -A seen=()

skip() {  # skip <name> <reason>
  skipped+=("$1|$2")
  echo "skipped $1 $2" >> "$report"
}

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  for entry in $line; do
    [[ -n "${seen[$entry]:-}" ]] && continue
    seen[$entry]=1
    if [[ "$entry" =~ $name_re ]]; then
      valid+=("$entry")
    else
      skip "$entry" invalid-name
    fi
  done
done < "$list"

if (( ${#valid[@]} > 0 )); then
  if ! "$apt_get" update -qq > "$log" 2>&1; then
    tail -5 "$log"
    for p in "${valid[@]}"; do skip "$p" apt-update-failed; done
    valid=()
  fi
fi

record_installed() {
  installed+=("$1")
  echo "installed $1" >> "$report"
}

if (( ${#valid[@]} > 0 )); then
  # One transaction first: the normal case, and the fastest.
  if "$apt_get" install -y --no-install-recommends "${valid[@]}" > "$log" 2>&1; then
    for p in "${valid[@]}"; do record_installed "$p"; done
  else
    # Something in the batch is bad, and apt refuses the whole transaction.
    # Retry one by one so the good packages still go in.
    for p in "${valid[@]}"; do
      if "$apt_get" install -y --no-install-recommends "$p" > "$log" 2>&1; then
        record_installed "$p"
      elif grep -qE "Unable to locate package|has no installation candidate|is not available" "$log"; then
        skip "$p" not-found
      else
        tail -3 "$log" | sed "s/^/    [$p] /"
        skip "$p" install-failed
      fi
    done
  fi
  rm -rf /var/lib/apt/lists/* 2>/dev/null || true
fi

reason_text() {
  case "$1" in
    invalid-name)      echo "not a valid Debian package name (lowercase letters, digits, + - . only)" ;;
    not-found)         echo "not in the package archive (a typo, or renamed in this Debian release?)" ;;
    install-failed)    echo "found, but apt could not install it (see the lines above)" ;;
    apt-update-failed) echo "apt-get update failed (no network during the build?)" ;;
    *)                 echo "$1" ;;
  esac
}

echo "==> local packages: ${#installed[@]} installed, ${#skipped[@]} skipped"
for entry in "${skipped[@]}"; do
  echo "    SKIPPED  ${entry%%|*}  — $(reason_text "${entry#*|}")"
done

if (( ${#skipped[@]} > 0 )) && [[ "$strict" == 1 || "$strict" == true ]]; then
  echo "LOCAL_PACKAGES_STRICT is set: failing the build over the skipped entries above." >&2
  exit 1
fi
exit 0
