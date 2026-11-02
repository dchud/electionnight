#!/usr/bin/env bash
# Download pinned frontend assets. Everything is served locally at runtime.
# Altair's generated specs fix the Vega and Vega-Lite major versions.
set -euo pipefail
cd "$(dirname "$0")/.."

VENDOR=src/monitor/web/static/vendor
CDN=https://cdn.jsdelivr.net/npm
TAILWIND_VERSION=v4.3.3

mkdir -p "$VENDOR" bin

fetch() {
  curl -fsSL --retry 3 "$CDN/$1" -o "$VENDOR/$2"
}

fetch htmx.org@2.0.11/dist/htmx.min.js htmx.min.js
fetch htmx-ext-sse@2.2.4/dist/sse.min.js sse.min.js
fetch vega@6.4.0/build/vega.min.js vega.min.js
fetch vega-lite@6.4.1/build/vega-lite.min.js vega-lite.min.js
fetch vega-embed@7.3.0/build/vega-embed.min.js vega-embed.min.js
fetch d3@7.9.0/dist/d3.min.js d3.min.js
fetch topojson-client@3.1.0/dist/topojson-client.min.js topojson-client.min.js
fetch @fontsource-variable/inter@5.3.0/files/inter-latin-wght-normal.woff2 \
  inter-latin.woff2
fetch @fontsource-variable/inter@5.3.0/files/inter-latin-ext-wght-normal.woff2 \
  inter-latin-ext.woff2

mkdir -p "$VENDOR/katex/fonts"
KATEX=katex@0.16.11/dist
fetch "$KATEX/katex.min.js" katex/katex.min.js
fetch "$KATEX/katex.min.css" katex/katex.min.css
fetch "$KATEX/contrib/auto-render.min.js" katex/auto-render.min.js
for font in \
  KaTeX_AMS-Regular.woff2 KaTeX_Caligraphic-Bold.woff2 \
  KaTeX_Caligraphic-Regular.woff2 KaTeX_Fraktur-Bold.woff2 \
  KaTeX_Fraktur-Regular.woff2 KaTeX_Main-Bold.woff2 \
  KaTeX_Main-BoldItalic.woff2 KaTeX_Main-Italic.woff2 \
  KaTeX_Main-Regular.woff2 KaTeX_Math-BoldItalic.woff2 \
  KaTeX_Math-Italic.woff2 KaTeX_SansSerif-Bold.woff2 \
  KaTeX_SansSerif-Italic.woff2 KaTeX_SansSerif-Regular.woff2 \
  KaTeX_Script-Regular.woff2 KaTeX_Size1-Regular.woff2 \
  KaTeX_Size2-Regular.woff2 KaTeX_Size3-Regular.woff2 \
  KaTeX_Size4-Regular.woff2 KaTeX_Typewriter-Regular.woff2; do
  fetch "$KATEX/fonts/$font" "katex/fonts/$font"
done

if [[ ! -x bin/tailwindcss ]]; then
  curl -fsSL --retry 3 -o bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION/tailwindcss-macos-arm64"
  chmod +x bin/tailwindcss
fi
ls -l "$VENDOR" | awk 'NR>1 {print $5, $9}'
