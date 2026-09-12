import argparse
import os
import re
import subprocess
import urllib.request
import urllib.parse
import zipfile
import sys
import hashlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from PIL import Image

def ensure_katex_assets(cache_dir="katex_assets"):
    """Ensures local KaTeX assets (JS, CSS, fonts) are downloaded and extracted."""
    katex_dir = os.path.join(cache_dir, "katex")
    if not os.path.exists(os.path.join(katex_dir, "katex.min.js")):
        os.makedirs(cache_dir, exist_ok=True)
        zip_path = os.path.join(cache_dir, "katex.zip")
        print("Downloading KaTeX for offline local caching...")
        urllib.request.urlretrieve("https://github.com/KaTeX/KaTeX/releases/download/v0.18.4/katex.zip", zip_path)
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(cache_dir)
        if os.path.exists(zip_path):
            os.remove(zip_path)
    return os.path.abspath(katex_dir)

# KaTeX HTML template with transparent background & tag layout fix
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <link rel="stylesheet" href="file://{katex_dir}/katex.min.css">
  <script src="file://{katex_dir}/katex.min.js"></script>
  <style>
    body {{
      margin: 0;
      padding: 10px;
      display: inline-block;
      background: transparent;
      white-space: nowrap;
    }}
    .katex {{ font-size: 3.5em; }}

    .katex-display {{
      display: flex !important;
      align-items: center;
      justify-content: space-between;
      gap: 2rem;
      margin: 0 !important;
    }}
    .katex-display > .katex {{
      margin-right: 0 !important;
    }}
    .katex-tag {{
      position: relative !important;
      display: inline-block !important;
      padding-left: 1.5rem;
    }}
  </style>
</head>
<body>
  <div id="math"></div>
  <script>
    katex.render(decodeURIComponent("{eq_encoded}"), document.getElementById('math'), {{
      displayMode: {display_mode}
    }});
  </script>
</body>
</html>
"""

def auto_crop_png(filename, padding=10):
    """Trims transparent margins around the rendered equation using the Alpha channel."""
    im = Image.open(filename)

    if im.mode != 'RGBA':
        im = im.convert('RGBA')

    alpha = im.split()[-1]
    bbox = alpha.getbbox()

    if bbox:
        left = max(0, bbox[0] - padding)
        top = max(0, bbox[1] - padding)
        right = min(im.width, bbox[2] + padding)
        bottom = min(im.height, bbox[3] + padding)

        cropped = im.crop((left, top, right, bottom))
        cropped.save(filename)

def render_math_worker(task):
    """Worker function executed in parallel across CPU cores."""
    equation, filename, display_mode, katex_dir = task

    temp_html = f"{filename}.html"
    eq_encoded = urllib.parse.quote(equation)

    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.format(katex_dir=katex_dir, eq_encoded=eq_encoded, display_mode="true" if display_mode else "false"))

    chrome_cmd = [
        "google-chrome",
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--default-background-color=00000000",
        "--allow-file-access-from-files",
        "--force-device-scale-factor=2",
        "--disable-background-networking",
        "--disable-component-update",
        "--host-rules=MAP * 127.0.0.1",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--window-size=3200,600",
        f"--screenshot={filename}",
        temp_html
    ]
    subprocess.run(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(filename):
        auto_crop_png(filename)

    if os.path.exists(temp_html):
        os.remove(temp_html)

def convert_md_to_epub(input_md, output_epub, img_dir="math_images", num_workers=None, force_render=False, resource_path=None):
    """Converts Markdown with LaTeX math to EPUB using parallel headless Chrome rendering."""
    os.makedirs(img_dir, exist_ok=True)
    katex_dir = ensure_katex_assets()

    # Default worker count matches os.cpu_count() (all logical CPU threads)
    cpu_count = num_workers if num_workers else (os.cpu_count() or 4)

    with open(input_md, "r", encoding="utf-8") as f:
        content = f.read()

    tasks_dict = {}

    def get_eq_filename(eq_text, display_mode):
        key = f"{'block' if display_mode else 'inline'}:{eq_text}".encode("utf-8")
        eq_hash = hashlib.md5(key).hexdigest()[:12]
        prefix = "eq_block" if display_mode else "eq_inline"
        return os.path.join(img_dir, f"{prefix}_{eq_hash}.png")

    # 1. Collect Block Math $$ ... $$
    def replace_block_math(match):
        eq_text = match.group(1).strip()
        png_path = get_eq_filename(eq_text, True)
        if png_path not in tasks_dict:
            tasks_dict[png_path] = (eq_text, png_path, True, katex_dir)
        return f"\n\n![equation]({png_path}){{.eq-block}}\n\n"

    # 2. Collect Inline Math $ ... $
    def replace_inline_math(match):
        eq_text = match.group(1).strip()
        png_path = get_eq_filename(eq_text, False)
        if png_path not in tasks_dict:
            tasks_dict[png_path] = (eq_text, png_path, False, katex_dir)
        return f"![equation]({png_path}){{.eq-inline}}"

    content = re.sub(r'\$\$(.*?)\$\$', replace_block_math, content, flags=re.DOTALL)
    content = re.sub(r'(?<!\$)\$([^$\n]+)\$(?!\$)', replace_inline_math, content)

    all_unique_tasks = list(tasks_dict.values())
    if force_render:
        tasks_to_render = all_unique_tasks
    else:
        tasks_to_render = [t for t in all_unique_tasks if not (os.path.exists(t[1]) and os.path.getsize(t[1]) > 0)]

    if all_unique_tasks and not force_render:
        cached_count = len(all_unique_tasks) - len(tasks_to_render)
        if cached_count > 0:
            print(f"Skipping {cached_count} already-rendered equation PNGs (use -f/--force to re-render)...")

    # Render all equation PNGs concurrently across available CPU threads
    if tasks_to_render:
        print(f"Rendering {len(tasks_to_render)} unique equations in parallel using {cpu_count} CPU worker processes...")
        with ProcessPoolExecutor(max_workers=cpu_count) as executor:
            futures = [executor.submit(render_math_worker, task) for task in tasks_to_render]
            total = len(futures)
            for i, future in enumerate(as_completed(futures), 1):
                future.result()
                sys.stdout.write(f"\rRendering equations: [{i}/{total}] {i / total * 100:.1f}%")
                sys.stdout.flush()
            print()

    temp_processed_md = "temp_processed.md"
    with open(temp_processed_md, "w", encoding="utf-8") as f:
        f.write(content)

    # Temporary EPUB CSS to handle image responsive scaling in KOReader
    epub_css_path = "epub_math.css"
    with open(epub_css_path, "w", encoding="utf-8") as f:
        f.write("""
        img.eq-block {
            max-width: 100%;
            height: auto;
            display: block;
            margin: 1em auto;
        }
        img.eq-inline {
            height: 1.45em;
            vertical-align: -0.22em;
            width: auto;
        }
        """)

    print(f"Compiling '{output_epub}' via Pandoc...")
    pandoc_cmd = [
        "pandoc", temp_processed_md,
        "-o", output_epub,
        "--css", epub_css_path,
        "--toc",
        "--toc-depth=3"
    ]
    if resource_path:
        # Where pandoc looks for images referenced by relative paths.
        pandoc_cmd += ["--resource-path", f"{resource_path}{os.pathsep}."]
    subprocess.run(pandoc_cmd)

    # Cleanup temporary working files
    for temp_file in [temp_processed_md, epub_css_path]:
        if os.path.exists(temp_file):
            os.remove(temp_file)

    print(f"Success! Built '{output_epub}'.")

def main():
    parser = argparse.ArgumentParser(
        description="Convert Markdown with LaTeX math to EPUB with rendered transparent PNG images for Kindle."
    )
    parser.add_argument("-i", "--input", required=True, help="Path to input .md file")
    parser.add_argument("-o", "--output", required=True, help="Path to output .epub file")
    parser.add_argument("--img-dir", default="math_images", help="Directory to store generated math PNGs (default: math_images)")
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Number of parallel Chrome render processes (default: all available CPU threads)")
    parser.add_argument("-f", "--force", action="store_true", help="Force re-rendering of all equations, ignoring cached PNGs")
    parser.add_argument("--resource-path", default=None, help="Directory pandoc should search for images referenced by relative paths (default: current directory only)")

    args = parser.parse_args()
    convert_md_to_epub(args.input, args.output, args.img_dir, args.jobs, force_render=args.force, resource_path=args.resource_path)

if __name__ == "__main__":
    main()
