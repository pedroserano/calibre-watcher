#!/usr/bin/env python3
"""
calibre_watcher.py  - runs in a container on your TrueNAS server (stdlib only).

Polls Calibre-Web's "Read Books" OPDS feed. When a book newly shows up as read it
records the exact time and grabs the cover, and adds it to a QUEUE. Your Mac
(calibre_to_dayone.py in queue mode) collects the queue whenever it is awake,
creates the Day One entries dated with the real detection time, and acknowledges
them. If the Mac is off for days, nothing is lost and the dates stay correct.

The first time it runs (no state yet) it only records the books already marked
read, without queueing them, so you don't get a flood of old entries.

Environment variables
  CALIBRE_URL         e.g. http://192.168.7.80:8083   (required)
  CALIBRE_USER / CALIBRE_PASSWORD                     (your Calibre-Web login)
  CALIBRE_READ_PATH   default /opds/readbooks
  QUEUE_TOKEN         shared secret the Mac must send (required)
  POLL_SECONDS        default 300
  PORT                default 8099
  DATA_DIR            default /data  (mount a persistent volume here)

HTTP API (LAN only; every request needs  Authorization: Bearer <QUEUE_TOKEN>)
  GET  /queue        list of pending entries
  GET  /cover/<id>   cover image for a pending entry
  POST /ack/<id>     remove an entry once the Mac has logged it
  GET  /sample       one read book + cover (not queued), for testing
  GET  /health       no auth
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"

CALIBRE_URL = os.environ.get("CALIBRE_URL", "").rstrip("/")
CALIBRE_USER = os.environ.get("CALIBRE_USER", "")
CALIBRE_PASSWORD = os.environ.get("CALIBRE_PASSWORD", "")
CALIBRE_READ_PATH = os.environ.get("CALIBRE_READ_PATH", "/opds/readbooks")
QUEUE_TOKEN = os.environ.get("QUEUE_TOKEN", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
PORT = int(os.environ.get("PORT", "8099"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
QUEUE_DIR = DATA_DIR / "queue"
SEEN_FILE = DATA_DIR / "seen.json"

lock = threading.Lock()


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Calibre-Web OPDS
# ---------------------------------------------------------------------------

def make_opener():
    handlers = []
    if CALIBRE_USER:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, CALIBRE_URL, CALIBRE_USER, CALIBRE_PASSWORD)
        handlers += [urllib.request.HTTPBasicAuthHandler(mgr), urllib.request.HTTPDigestAuthHandler(mgr)]
    return urllib.request.build_opener(*handlers)


OPENER = None


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "calibre-watcher/1.0"})
    with OPENER.open(req, timeout=timeout) as r:
        return r.read()


def absolute(href):
    return urllib.parse.urljoin(CALIBRE_URL + "/", href)


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    books, next_href = [], None
    for link in root.findall(f"{ATOM}link"):
        if link.get("rel") == "next":
            next_href = link.get("href")
    for entry in root.findall(f"{ATOM}entry"):
        title = (entry.findtext(f"{ATOM}title") or "Untitled").strip()
        authors = [a.findtext(f"{ATOM}name", "").strip() for a in entry.findall(f"{ATOM}author")]
        authors = [a for a in authors if a] or ["Unknown"]
        cover = thumb = None
        for link in entry.findall(f"{ATOM}link"):
            rel, href = link.get("rel", ""), link.get("href", "")
            if rel == "http://opds-spec.org/image":
                cover = href
            elif rel == "http://opds-spec.org/image/thumbnail":
                thumb = href
        key = (entry.findtext(f"{ATOM}id") or "").strip() or f"{title}|{authors[0]}"
        books.append({"key": key, "title": title, "authors": authors, "cover_href": cover or thumb})
    return books, next_href


def discover_read_feed():
    root = ET.fromstring(http_get(absolute("/opds")))
    for entry in root.findall(f"{ATOM}entry"):
        for link in entry.findall(f"{ATOM}link"):
            if "readbooks" in link.get("href", "").lower():
                return absolute(link.get("href"))
    return None


def fetch_read_books():
    url, seen_urls, books, first = absolute(CALIBRE_READ_PATH), set(), [], True
    while url and url not in seen_urls and len(seen_urls) < 200:
        seen_urls.add(url)
        try:
            data = http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404 and first:
                url = discover_read_feed()
                if not url:
                    raise
                seen_urls.add(url)
                data = http_get(url)
            else:
                raise
        first = False
        page, next_href = parse_feed(data)
        books += page
        url = absolute(next_href) if next_href else None
    out, keys = [], set()
    for b in books:
        if b["key"] not in keys:
            keys.add(b["key"])
            out.append(b)
    return out


def detect_image(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "heic"
    return None


IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif",
               "webp": "image/webp", "heic": "image/heic"}


def fetch_cover(book):
    if not book.get("cover_href"):
        return None, None
    try:
        data = http_get(absolute(book["cover_href"]))
    except Exception as e:  # noqa: BLE001
        log(f"cover fetch failed for {book['title']!r}: {e}")
        return None, None
    ext = detect_image(data)
    if not ext:
        log(f"cover for {book['title']!r} is not an image ({len(data)} bytes)")
        return None, None
    return data, ext


# ---------------------------------------------------------------------------
# State + queue
# ---------------------------------------------------------------------------

def item_id(key):
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def load_seen():
    return json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else None


def save_seen(seen):
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1))
    tmp.replace(SEEN_FILE)


def poll_once():
    books = fetch_read_books()
    with lock:
        seen = load_seen()
        if seen is None:  # first run: seed, don't queue
            seen = {b["key"]: {"title": b["title"], "seeded": True} for b in books}
            save_seen(seen)
            log(f"first run: recorded {len(books)} already-read book(s), nothing queued")
            return
        new = [b for b in books if b["key"] not in seen]
    for b in new:
        cover, ext = fetch_cover(b)
        iid = item_id(b["key"])
        detected = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta = {"id": iid, "key": b["key"], "title": b["title"], "authors": b["authors"],
                "detected_at": detected, "cover_ext": ext}
        with lock:
            if cover:
                (QUEUE_DIR / f"{iid}.img").write_bytes(cover)
            (QUEUE_DIR / f"{iid}.json").write_text(json.dumps(meta))
            seen = load_seen() or {}
            seen[b["key"]] = {"title": b["title"], "queued": detected}
            save_seen(seen)
        log(f"queued: {b['title']!r} by {', '.join(b['authors'])} (cover: {'yes' if cover else 'no'})")


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001
            log(f"poll failed: {e}")
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "calibre-watcher/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got, f"Bearer {QUEUE_TOKEN}")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/queue":
            with lock:
                items = [json.loads(p.read_text()) for p in sorted(QUEUE_DIR.glob("*.json"))]
            items.sort(key=lambda m: m["detected_at"])
            return self._send(200, items)
        if self.path.startswith("/cover/"):
            iid = self.path.split("/", 2)[2]
            if not iid.isalnum():
                return self._send(400, {"error": "bad id"})
            p = QUEUE_DIR / f"{iid}.img"
            if not p.exists():
                return self._send(404, {"error": "no cover"})
            data = p.read_bytes()
            ext = detect_image(data) or "jpg"
            return self._send(200, data, IMAGE_TYPES[ext],
                              {"Content-Disposition": f'inline; filename="cover.{ext}"'})
        if self.path == "/sample":
            try:
                books = fetch_read_books()
                if not books:
                    return self._send(404, {"error": "no read books in Calibre-Web"})
                b = books[0]
                cover, ext = fetch_cover(b)
                return self._send(200, {"title": b["title"], "authors": b["authors"],
                                        "cover_b64": base64.b64encode(cover).decode() if cover else None})
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"calibre-web: {e}"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path.startswith("/ack/"):
            iid = self.path.split("/", 2)[2]
            if not iid.isalnum():
                return self._send(400, {"error": "bad id"})
            with lock:
                (QUEUE_DIR / f"{iid}.json").unlink(missing_ok=True)
                (QUEUE_DIR / f"{iid}.img").unlink(missing_ok=True)
            log(f"acknowledged {iid}")
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def main():
    global OPENER
    if not CALIBRE_URL:
        sys.exit("CALIBRE_URL is required")
    if not QUEUE_TOKEN:
        sys.exit("QUEUE_TOKEN is required (any long random string; the Mac uses the same one)")
    OPENER = make_opener()
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=poll_loop, daemon=True).start()
    log(f"watching {CALIBRE_URL}{CALIBRE_READ_PATH} every {POLL_SECONDS}s; queue API on port {PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
