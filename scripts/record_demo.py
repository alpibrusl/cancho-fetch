#!/usr/bin/env python3
"""Record the landing page's demo as site/demo.{mp4,webm,poster.jpg}.

The recording is of the generated page, so run it after `scripts/site_demo.py`; it is a
file kept in the repository (not made by CI), and the page says it is a recording.

    python3 scripts/record_demo.py [--site site] [--out site]

Needs Playwright for Python with a Chromium (CHROMIUM=/path/to/chrome if it is not found) and ffmpeg (with libx264 and libvpx-vp9).
"""
import argparse
import functools
import http.server
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def chromium():
    for c in [os.environ.get("CHROMIUM"), *sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))]:
        if c and Path(c).exists():
            return str(c)
    return None


class Recorder:
    """Frames are screenshots with their times, so the video keeps the real timing (the browser's own video capture is not needed)."""

    def __init__(self, page, folder):
        self.page, self.folder, self.frames, self.t0 = page, folder, [], time.monotonic()

    def snap(self):
        n = len(self.frames)
        self.page.screenshot(path=str(self.folder / f"f{n:05d}.jpg"), type="jpeg", quality=85)
        self.frames.append(time.monotonic() - self.t0)

    def wait(self, ms):
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            self.snap()

    def wait_done(self, extra=1200):
        deadline = time.monotonic() + 60
        while not self.page.evaluate("window.cdemo && window.cdemo.done()"):
            self.snap()
            if time.monotonic() > deadline:
                raise SystemExit("record_demo: the demo did not finish")
        self.wait(extra)

    def encode(self, out):
        lines = []
        for k, t in enumerate(self.frames):
            nxt = self.frames[k + 1] if k + 1 < len(self.frames) else t + 0.1
            lines += [f"file 'f{k:05d}.jpg'", f"duration {nxt - t:.4f}"]
        lines.append(f"file 'f{len(self.frames) - 1:05d}.jpg'")
        (self.folder / "list.txt").write_text("\n".join(lines) + "\n")
        base = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(self.folder / "list.txt"), "-vf", "fps=20,format=yuv420p"]
        subprocess.run(base + ["-c:v", "libx264", "-crf", "30", "-preset", "slow", "-movflags", "+faststart", "-an", str(out / "demo.mp4")], check=True)
        subprocess.run(base + ["-c:v", "libvpx-vp9", "-crf", "38", "-b:v", "0", "-an", str(out / "demo.webm")], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=str(ROOT / "site"))
    ap.add_argument("--out", default=str(ROOT / "site"))
    a = ap.parse_args()
    site, out = Path(a.site), Path(a.out)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(site), **kw)

        def log_message(self, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium(), args=["--no-sandbox"])
        page = browser.new_context(viewport={"width": 1280, "height": 720}, color_scheme="light").new_page()
        page.goto(f"http://127.0.0.1:{httpd.server_address[1]}/index.html")
        page.wait_for_timeout(500)
        rec = Recorder(page, tmp)
        page.evaluate("window.scrollTo({top: document.querySelector('#demo h2').getBoundingClientRect().top + scrollY - 76, behavior: 'instant'})")
        rec.wait(800)
        page.click("#replay")
        rec.wait_done(2500)
        page.screenshot(path=str(out / "demo.poster.jpg"), type="jpeg", quality=82)
        page.evaluate("document.querySelector('#audience-section').scrollIntoView({behavior:'smooth'})")
        rec.wait(1500)
        page.click("#audience button[data-panel=agents]")
        rec.wait(2500)
        page.evaluate("document.querySelector('#alpha').scrollIntoView({behavior:'smooth'})")
        rec.wait(3000)
        browser.close()
    httpd.shutdown()
    rec.encode(out)
    shutil.rmtree(tmp)
    for f in ("demo.mp4", "demo.webm", "demo.poster.jpg"):
        print(f, (out / f).stat().st_size // 1024, "KiB")


if __name__ == "__main__":
    main()
