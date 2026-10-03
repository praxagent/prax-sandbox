#!/usr/bin/env bash
# ensure-image — build the sandbox image if it is missing, or if
# sandbox/local-packages.txt changed since it was built. Otherwise do nothing:
# safe to run on every start and every deploy.
#
# The image records which list it was built from in the label
# prax.local-packages (a hash of the list's package names, ignoring comments
# and blank lines; empty when there is no list). An image built before this
# label existed reads as empty too, so having no list never triggers a rebuild.
#
# After a build it prints what the list installed and what it skipped.
#
# Usage: scripts/ensure-image.sh [--rebuild]
#   SANDBOX_IMAGE           image tag (default prax-sandbox:latest)
#   LOCAL_PACKAGES_STRICT   1 = a skipped package fails the build
#   DOCKER                  docker binary (tests point it at a fake)
# Respects DOCKER_HOST like any docker command.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${SANDBOX_IMAGE:-prax-sandbox:latest}"
docker="${DOCKER:-docker}"
list="$root/sandbox/local-packages.txt"
force=false
[[ "${1:-}" == "--rebuild" ]] && force=true

wanted=""
if [[ -f "$list" ]]; then
  names="$(sed -e 's/#.*//' "$list" | tr -s '[:space:]' '\n' | grep -v '^$' || true)"
  [[ -n "$names" ]] && wanted="$(printf '%s\n' "$names" | sha256sum | cut -c1-16)"
fi

have=""
exists=true
if ! have="$("$docker" image inspect -f '{{ index .Config.Labels "prax.local-packages" }}' "$image" 2>/dev/null)"; then
  exists=false
  have=""
fi
[[ "$have" == "<no value>" ]] && have=""

if $exists && ! $force && [[ "$have" == "$wanted" ]]; then
  echo "==> sandbox image $image is current (local packages unchanged)"
  exit 0
fi

if ! $exists; then
  echo "==> building $image (not found; first build can take a while)"
elif $force; then
  echo "==> rebuilding $image"
else
  echo "==> rebuilding $image: sandbox/local-packages.txt changed"
fi

"$docker" build \
  --build-arg "LOCAL_PACKAGES_SHA=$wanted" \
  --build-arg "LOCAL_PACKAGES_STRICT=${LOCAL_PACKAGES_STRICT:-0}" \
  -t "$image" "$root/sandbox"

report="$("$docker" run --rm --entrypoint cat "$image" /etc/prax-sandbox/local-packages.report 2>/dev/null || true)"
installed="$(grep -c '^installed ' <<< "$report" || true)"
skipped="$(grep '^skipped ' <<< "$report" || true)"
if [[ -z "$report" ]]; then
  echo "==> local packages: none"
elif [[ -z "$skipped" ]]; then
  echo "==> local packages: $installed installed, none skipped"
else
  echo "==> local packages: $installed installed, $(wc -l <<< "$skipped") SKIPPED:"
  sed 's/^skipped /    /' <<< "$skipped"
  echo "    (fix the names in sandbox/local-packages.txt, then run this again)"
fi
