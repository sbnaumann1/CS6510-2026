#!/usr/bin/env bash
# Render the architecture diagram's Mermaid source to docs/architecture.svg (spec 008 FR-023).
#
#   ./scripts/render_diagram.sh
#
# The flowchart is the first ```mermaid block of docs/architecture.md. The SVG is committed
# beside it as a fallback for viewers that do not render Mermaid; GitHub renders the block in
# place, so the SVG is only a copy. Needs Node (npx); the Mermaid CLI version is pinned.
set -euo pipefail
cd "$(dirname "$0")/.."

MMDC_VERSION="${MMDC_VERSION:-11.4.2}"
SRC=docs/architecture.md
OUT=docs/architecture.svg
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The first mermaid block (the flowchart) only; the sequence diagram stays in the markdown.
python3 - "$SRC" "$TMP/architecture.mmd" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
block = re.search(r"```mermaid\n(.*?)```", text, re.S)
if block is None:
    sys.exit("no mermaid block in " + sys.argv[1])
open(sys.argv[2], "w").write(block.group(1))
PY

cat > "$TMP/puppeteer.json" <<'JSON'
{ "args": ["--no-sandbox"] }
JSON

npx --yes "@mermaid-js/mermaid-cli@${MMDC_VERSION}" \
  -i "$TMP/architecture.mmd" -o "$OUT" -p "$TMP/puppeteer.json" -b transparent -w 1800
echo "wrote $OUT"
