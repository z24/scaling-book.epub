#!/usr/bin/env bash
#
# Build scaling-book.epub from the upstream jax-ml/scaling-book repo:
# clone/update it, flatten the chapters into one markdown, render the math, and
# compile the EPUB into this folder. The checkout is left exactly as cloned.
#
#   ./build_epub.sh [args forwarded to convert_md_to_epub.py, e.g. -f, -j 8]
#
# Needs git, pandoc, google-chrome, and a python3 with Pillow (PYTHON=... to
# pick an interpreter). Delete math_images/ to discard the equation cache.
#
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

REPO_URL="${REPO_URL:-https://github.com/jax-ml/scaling-book.git}"
BRANCH="${BRANCH:-main}"
PYTHON="${PYTHON:-python3}"

die() { echo "ERROR: $*" >&2; exit 1; }

for cmd in git pandoc google-chrome; do
  command -v "$cmd" >/dev/null || die "'$cmd' not found in PATH; please install it."
done
"$PYTHON" -c 'import PIL' 2>/dev/null \
  || die "'$PYTHON' is missing or has no Pillow; run 'pip install pillow' or set PYTHON=/path/to/python."

if [[ -d scaling-book/.git ]]; then
  echo "==> Updating scaling-book ($BRANCH)"
  git -C scaling-book fetch --depth 1 origin "$BRANCH"
  git -C scaling-book reset --hard FETCH_HEAD
else
  echo "==> Cloning $REPO_URL"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" scaling-book
fi

# convert_to_single_md.py always writes inside its own repo, so move the result
# out. The book text is copied verbatim; only EPUB metadata is prepended, since
# pandoc would otherwise title the book "temp_processed".
echo "==> Generating combined markdown"
"$PYTHON" scaling-book/bin/convert_to_single_md.py
cat > scaling-book-combined.md <<'EOF'
---
title: "How to Scale Your Model"
subtitle: "A Systems View of LLMs on TPUs"
author: "Jacob Austin et al., Google DeepMind"
lang: en-US
---

EOF
cat scaling-book/scaling-book-combined.md >> scaling-book-combined.md
rm -f scaling-book/scaling-book-combined.md

# --resource-path is how pandoc resolves the book's relative assets/ links from
# here, so neither the markdown nor the checkout needs to be touched.
echo "==> Building scaling-book.epub (first run renders ~800 equations)"
"$PYTHON" convert_md_to_epub.py \
  -i scaling-book-combined.md \
  -o scaling-book.epub \
  --img-dir math_images \
  --resource-path scaling-book "$@"

# convert_md_to_epub.py reports success even when pandoc fails, so verify.
[[ -s scaling-book.epub ]] || die "pandoc did not produce scaling-book.epub."
echo "==> Done: $PWD/scaling-book.epub ($(du -h scaling-book.epub | cut -f1))"
