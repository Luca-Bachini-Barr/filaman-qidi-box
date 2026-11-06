#!/bin/sh
# Build the zip that FilaMan's plugin manager installs.
set -e
cd "$(dirname "$0")"
version=$(python3 -c 'import json; print(json.load(open("qidi_box/plugin.json"))["version"])')
mkdir -p dist
out="dist/qidi_box-$version.zip"
rm -f "$out"
python3 - "$out" <<'PY'
import sys
import zipfile
from pathlib import Path

with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(Path("qidi_box").iterdir()):
        if path.suffix in {".py", ".json"}:
            archive.write(path)
PY
echo "$out"
