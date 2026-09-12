#!/usr/bin/env bash
# MergePilot offline image bundle EXPORTER.
#
# Produces (in --out dir, default repo root):
#   MergePilot-images-linux-amd64.tar.zst   zstd-compressed docker save archive (ALL 9 images)
#   MergePilot-images-manifest.json         image/tag/digest/arch/commit/version manifest
#   SHA256SUMS                              sha256 of the archive + the manifest
#
# Run inside WSL2 (or any Linux) with a running Docker engine, from a repo checkout.
# Requires: docker, zstd, python3, sha256sum, git. Never embeds secrets: the manifest
# is generated from docker metadata + git only.
#
# Usage:
#   RELEASE_VERSION=v0.2.1 ./export-images.sh [--out DIR]
#
# Env:
#   RELEASE_VERSION   required, must not be "latest"
#   SOURCE_COMMIT     optional override (default: git rev-parse HEAD)
#   ZSTD_LEVEL        optional (default: 3; 19 = smallest, slower)

set -euo pipefail

RELEASE_VERSION="${RELEASE_VERSION:-}"
[[ -n "$RELEASE_VERSION" && "$RELEASE_VERSION" != "latest" ]] || {
  echo "ERROR: RELEASE_VERSION is required and must not be 'latest'" >&2; exit 2
}

OUT_DIR="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_SET="$SCRIPT_DIR/image-set.json"
[[ -f "$IMAGE_SET" ]] || { echo "ERROR: image-set.json not found next to this script" >&2; exit 2; }

# ---- diagnostics: required tools -------------------------------------------------
missing=()
command -v docker    >/dev/null 2>&1 || missing+=("docker")
command -v zstd      >/dev/null 2>&1 || missing+=("zstd (Ubuntu/WSL: apt-get install zstd)")
command -v python3   >/dev/null 2>&1 || command -v python >/dev/null 2>&1 || missing+=("python3 (or python)")
command -v sha256sum >/dev/null 2>&1 || missing+=("sha256sum")
command -v git       >/dev/null 2>&1 || missing+=("git")
if (( ${#missing[@]} )); then
  echo "ERROR: missing required tools: ${missing[*]}" >&2; exit 2
fi

docker version --format '{{.Server.Os}}/{{.Server.Arch}}' >/dev/null 2>&1 || {
  echo "ERROR: Docker engine is not reachable. Start Docker Desktop (or the docker daemon)." >&2
  exit 2
}

SERVER_PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
[[ "$SERVER_PLATFORM" == "linux/amd64" ]] || {
  echo "ERROR: engine platform is $SERVER_PLATFORM; this bundle is linux/amd64." >&2
  echo "       Do not ship a mixed-architecture bundle; build on a linux/amd64 engine." >&2
  exit 2
}

# ---- canonical image set ----------------------------------------------------------
PYBIN="$(command -v python3 || command -v python)"
REFS_COUNT="$("${PYBIN}" -c 'import json,sys;d=json.load(open(sys.argv[1],encoding="utf-8"));print(len(d["images"]["built"])+len(d["images"]["pinned_remote"]))' "$IMAGE_SET")"
PLATFORM="$("${PYBIN}" -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["platform"])' "$IMAGE_SET")"

REFS=()
while IFS= read -r ref; do REFS+=("$ref"); done < <(
  "${PYBIN}" -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for i in d["images"]["built"]:
    print(i["image"])
for i in d["images"]["pinned_remote"]:
    print(i["ref"])
' "$IMAGE_SET"
)

# every image must exist locally (built via docker compose build / mergepilot install;
# pinned remote via docker pull)
absent=()
for ref in "${REFS[@]}"; do
  docker image inspect "$ref" > /dev/null 2>&1 || absent+=("$ref")
done
if (( ${#absent[@]} )); then
  echo "ERROR: ${#absent[@]} image(s) not present locally. Build or pull first:" >&2
  for a in "${absent[@]}"; do echo "  $a" >&2; done
  echo "  built images: docker compose build   |   pinned remote: docker pull <ref>" >&2
  exit 2
fi

SOURCE_COMMIT="${SOURCE_COMMIT:-$(git rev-parse HEAD)}"
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- export: docker save | zstd (streaming, no intermediate tar) ------------------
ARCHIVE="$OUT_DIR/MergePilot-images-linux-amd64.tar.zst"
echo ">> exporting $REFS_COUNT images -> $ARCHIVE"
docker save "${REFS[@]}" | zstd -q -T0 "-${ZSTD_LEVEL:-3}" -o "$ARCHIVE"
echo ">> archive sha256: $(sha256sum "$ARCHIVE" | cut -c1-16)... ($(stat -c %s "$ARCHIVE") bytes)"

# ---- manifest (docker metadata + git only; secrets cannot enter) ------------------
MANIFEST="$OUT_DIR/MergePilot-images-manifest.json"
"${PYBIN}" - "$IMAGE_SET" "$MANIFEST" "$PLATFORM" "$SOURCE_COMMIT" "$RELEASE_VERSION" "$CREATED_AT" <<'PYEOF'
import datetime, json, subprocess, sys

image_set, out, platform, commit, version, created = sys.argv[1:7]

def inspect(ref, fmt):
    r = subprocess.run(["docker", "inspect", "--format", fmt, ref], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("docker inspect failed for " + ref)
    return r.stdout.strip()

d = json.load(open(image_set, encoding="utf-8"))
images = []
for i in d["images"]["built"]:
    images.append({
        "image": i["image"].rsplit(":", 1)[0],
        "tag": i["image"].rsplit(":", 1)[1],
        "ref": i["image"],
        "digest": inspect(i["image"], "{{.Id}}"),
        "repo_digests": json.loads(inspect(i["image"], "{{json .RepoDigests}}") or "[]"),
        "architecture": inspect(i["ref"], "{{.Os}}") + "/" + inspect(i["ref"], "{{.Architecture}}"),
        "source": "built: " + i["dockerfile"],
    })
for i in d["images"]["pinned_remote"]:
    images.append({
        "image": i["image"],
        "tag": i["tag"],
        "ref": i["ref"],
        "digest": inspect(i["ref"], "{{.Id}}"),
        "repo_digests": [i["repo_digest"]],
        "manifest_digest": i["manifest_digest"],
        "architecture": inspect(i["ref"], "{{.Os}}") + "/" + inspect(i["ref"], "{{.Architecture}}"),
        "source": "upstream, digest-pinned (the only remote image in the stack)",
    })
manifest = {
    "schema_version": 1,
    "bundle": "MergePilot-images-linux-amd64",
    "archive": "MergePilot-images-linux-amd64.tar.zst",
    "platform": platform,
    "source_commit": commit,
    "release_version": version,
    "created_at": created,
    "images": images,
    "verification": {
        "rule": "after docker load, for every images[] entry: docker inspect --format '{{.Id}}' <ref> MUST equal digest",
        "note": "config digests survive docker save/load; RepoDigests do not",
    },
}
with open(out, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)
print("manifest images:", len(images))
PYEOF
echo ">> manifest: $MANIFEST"

# ---- SHA256SUMS ------------------------------------------------------------------
SUMS="$OUT_DIR/SHA256SUMS"
( cd "$OUT_DIR" && sha256sum "$(basename "$ARCHIVE")" "$(basename "$MANIFEST")" > "$(basename "$SUMS")" )
echo ">> SHA256SUMS: $SUMS"
echo "DONE. Distribute: $ARCHIVE + $MANIFEST + $SUMS (upload as Release assets; never commit them)"
