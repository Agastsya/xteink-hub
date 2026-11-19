#!/usr/bin/env python3
"""
Xteink Hub - one app on your Android phone (Termux) for the Xteink X4 + CrossPoint:
  * OPDS server       -> browse & download on the X4 (File Transfer -> OPDS Browser)
  * Live articles     -> sci-fi + science RSS feeds turned into EPUB digests
  * Free books        -> public-domain sci-fi / science from Project Gutenberg
  * KOReader sync     -> reading-progress sync server for CrossPoint's KOReader Sync
  * Phone dashboard   -> http://127.0.0.1:8080  (buttons for everything)
"""
import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import zipfile
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs, unquote
from xml.sax.saxutils import escape

import feedparser
import requests
from bs4 import BeautifulSoup

HOME = Path(os.environ.get("XHUB_HOME", str(Path.home() / "xteink-hub"))).expanduser()
PORT = int(os.environ.get("XHUB_PORT", "8080"))
REMOTE_ADMIN = os.environ.get("XHUB_REMOTE_ADMIN") == "1"   # allow dashboard buttons from other devices

CATS = {"scifi": "Sci-Fi", "science": "Science", "other": "Other"}
DEFAULT_CONFIG = {
    "feeds": [
        {"cat": "scifi", "name": "Reactor (Tor.com)", "url": "https://reactormag.com/feed/"},
        {"cat": "scifi", "name": "Clarkesworld", "url": "https://clarkesworldmagazine.com/feed/"},
        {"cat": "scifi", "name": "Strange Horizons", "url": "https://strangehorizons.com/feed/"},
        {"cat": "scifi", "name": "Lightspeed", "url": "https://www.lightspeedmagazine.com/feed/"},
        {"cat": "science", "name": "Quanta Magazine", "url": "https://www.quantamagazine.org/feed/"},
        {"cat": "science", "name": "Ars Technica Science", "url": "https://feeds.arstechnica.com/arstechnica/science"},
        {"cat": "science", "name": "NASA", "url": "https://www.nasa.gov/feed/"},
        {"cat": "science", "name": "Phys.org", "url": "https://phys.org/rss-feed/"},
        {"cat": "science", "name": "ScienceDaily", "url": "https://www.sciencedaily.com/rss/all.xml"},
    ],
    "book_topics": [
        {"cat": "scifi", "topic": "science fiction"},
        {"cat": "scifi", "topic": "science fiction short stories"},
        {"cat": "science", "topic": "science"},
        {"cat": "science", "topic": "astronomy"},
        {"cat": "science", "topic": "natural history"},
    ],
    "articles_per_feed": 8,
    "books_per_topic": 15,
    "keep_days": 7,
    "auto_refresh_hours": 0,
    "allow_sync_signup": True,
}

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (Linux; Android) xteink-hub/2.0"

LOG = deque(maxlen=300)
STATE = {"busy": False, "task": "", "last_update": None}
LOCK = threading.Lock()
DB_LOCK = threading.Lock()


# ------------------------------------------------------------------ basics
def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    LOG.append(line)
    print(line, flush=True)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def get(url, **kw):
    r = SESSION.get(url, timeout=25, **kw)
    r.raise_for_status()
    return r


def dirs():
    for d in ("files/articles", "files/books", "files/mine", "meta/articles",
              "meta/books", "meta/mine", "opds"):
        (HOME / d).mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def cfg():
    c = load_json(HOME / "config.json", None)
    if not c:
        c = json.loads(json.dumps(DEFAULT_CONFIG))
        save_json(HOME / "config.json", c)
    for k, v in DEFAULT_CONFIG.items():
        c.setdefault(k, v)
    return c


# ------------------------------------------------------------------ article extraction
def clean_blocks(root):
    out = []
    if root is None:
        return out
    for el in root.find_all(["h2", "h3", "h4", "p", "blockquote", "li"]):
        if el.name in ("blockquote", "li") and el.find("p"):
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if not text:
            continue
        if el.name.startswith("h"):
            kind = "h"
        elif el.name == "blockquote":
            kind = "q"
        elif el.name == "li":
            kind, text = "p", "• " + text
        else:
            if len(text) < 25:
                continue
            kind = "p"
        if out and out[-1] == (kind, text):
            continue
        out.append((kind, text))
    return out


def extract_article(url, fallback_html):
    try:
        soup = BeautifulSoup(get(url).text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "aside", "form",
                       "figure", "noscript", "iframe", "button"]):
            t.decompose()
        box = soup.find("article") or soup.find("main") or soup.body
        blocks = clean_blocks(box)
        if sum(len(t) for _, t in blocks) > 800:
            return blocks
    except Exception as e:
        log("   page fetch failed, using feed text:", type(e).__name__)
    return clean_blocks(BeautifulSoup(fallback_html or "", "html.parser"))


# ------------------------------------------------------------------ EPUB writer
CSS = ("body{font-family:serif;line-height:1.4;margin:0 .5em}h1,h2,h3{font-family:sans-serif}"
       "p{margin:.6em 0;text-align:left}blockquote{margin:.8em 1.2em;font-style:italic}")


def blocks_to_xhtml(title, blocks):
    body = [f"<h2>{escape(title)}</h2>"]
    for kind, text in blocks:
        t = escape(text)
        body.append(f"<h3>{t}</h3>" if kind == "h" else
                    f"<blockquote><p>{t}</p></blockquote>" if kind == "q" else f"<p>{t}</p>")
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            f'<head><title>{escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/>'
            f'</head><body>{"".join(body)}</body></html>')


def build_epub(path, title, author, chapters):
    uid = "urn:xteink:" + hashlib.sha1((title + author).encode()).hexdigest()[:16]
    items, spine, navpoints, navli = [], [], [], []
    for i, (ct, _) in enumerate(chapters, 1):
        items.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
        navpoints.append(f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{escape(ct)}</text>'
                         f'</navLabel><content src="c{i}.xhtml"/></navPoint>')
        navli.append(f'<li><a href="c{i}.xhtml">{escape(ct)}</a></li>')
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{escape(title)}</dc:title>'
           f'<dc:creator>{escape(author)}</dc:creator><dc:language>en</dc:language>'
           f'<meta property="dcterms:modified">{now_iso()}</meta></metadata><manifest>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
           '<item id="css" href="style.css" media-type="text/css"/>'
           + "".join(items) + '</manifest><spine toc="ncx">' + "".join(spine) + '</spine></package>')
    ncx = ('<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
           f'version="2005-1"><head><meta name="dtb:uid" content="{uid}"/></head><docTitle><text>'
           f'{escape(title)}</text></docTitle><navMap>' + "".join(navpoints) + '</navMap></ncx>')
    nav = ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
           f'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>{escape(title)}</title></head><body>'
           '<nav epub:type="toc"><h1>Contents</h1><ol>' + "".join(navli) + '</ol></nav></body></html>')
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    tmp = Path(str(path) + ".part")
    D = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=D)
        z.writestr("OEBPS/content.opf", opf, compress_type=D)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=D)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=D)
        z.writestr("OEBPS/style.css", CSS, compress_type=D)
        for i, (ct, blocks) in enumerate(chapters, 1):
            z.writestr(f"OEBPS/c{i}.xhtml", blocks_to_xhtml(ct, blocks), compress_type=D)
    tmp.replace(path)


# ------------------------------------------------------------------ content updates
def write_meta(kind, fname, data):
    save_json(HOME / "meta" / kind / (fname + ".json"), data)


def update_articles(c):
    today = dt.date.today().isoformat()
    for f in c["feeds"]:
        name, url, cat = f["name"], f["url"], f.get("cat", "other")
        fname = f"{slug(name)}-{today}.epub"
        try:
            log("Feed:", name)
            parsed = feedparser.parse(get(url).content)
            chapters = []
            for e in parsed.entries[: int(c["articles_per_feed"])]:
                fb = (e.get("content") or [{}])[0].get("value") or e.get("summary", "")
                blocks = extract_article(e.get("link", ""), fb)
                if blocks:
                    chapters.append((e.get("title", "Untitled"), blocks))
                time.sleep(1)
            if not chapters:
                log("   nothing usable, skipped")
                continue
            build_epub(HOME / "files/articles" / fname, f"{name} - {today}", name, chapters)
            write_meta("articles", fname, {"title": f"{name} - {today}", "author": name, "cat": cat,
                                           "date": today, "summary": f"{len(chapters)} articles"})
            log(f"   built ({len(chapters)} articles)")
        except Exception as e:
            log("   feed failed:", type(e).__name__, e)
    cutoff = (dt.date.today() - dt.timedelta(days=int(c["keep_days"]))).isoformat()
    for m in (HOME / "meta/articles").glob("*.json"):
        if load_json(m, {}).get("date", "9999") < cutoff:
            (HOME / "files/articles" / m.name[:-5]).unlink(missing_ok=True)
            m.unlink(missing_ok=True)


def update_books(c):
    for bt in c["book_topics"]:
        topic, cat = bt["topic"], bt.get("cat", "other")
        url = "https://gutendex.com/books/?languages=en&sort=popular&topic=" + quote(topic)
        got = 0
        log("Books:", topic)
        while url and got < int(c["books_per_topic"]):
            try:
                data = get(url).json()
            except Exception as e:
                log("   gutendex failed:", type(e).__name__)
                break
            for b in data.get("results", []):
                if got >= int(c["books_per_topic"]):
                    break
                epub = b.get("formats", {}).get("application/epub+zip")
                if not epub:
                    continue
                got += 1
                fname = f"pg{b['id']}.epub"
                fpath = HOME / "files/books" / fname
                if fpath.exists():
                    continue
                try:
                    tmp = Path(str(fpath) + ".part")
                    tmp.write_bytes(get(epub).content)
                    tmp.replace(fpath)
                    author = b["authors"][0]["name"] if b.get("authors") else "Unknown"
                    write_meta("books", fname, {"title": b["title"], "author": author, "cat": cat})
                    log("   +", b["title"][:60])
                    time.sleep(1)
                except Exception as e:
                    log("   download failed:", b.get("id"), type(e).__name__)
            url = data.get("next")


def run_update(what="all"):
    if not LOCK.acquire(blocking=False):
        return False

    def work():
        try:
            STATE.update(busy=True, task=f"Updating {what}")
            c = cfg()
            if what in ("all", "articles"):
                update_articles(c)
            if what in ("all", "books"):
                update_books(c)
            build_catalog()
            STATE["last_update"] = time.time()
            save_json(HOME / "state.json", {"last_update": STATE["last_update"]})
            log("Update finished.")
        except Exception as e:
            log("Update failed:", e)
        finally:
            STATE.update(busy=False, task="")
            LOCK.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def auto_refresher():
    while True:
        time.sleep(60)
        h = float(cfg().get("auto_refresh_hours") or 0)
        last = STATE.get("last_update") or 0
        if h > 0 and time.time() - last >= h * 3600 and not STATE["busy"]:
            log("Auto refresh")
            run_update("all")


# ------------------------------------------------------------------ OPDS
NAV_T = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_T = "application/atom+xml;profile=opds-catalog;kind=acquisition"


def atom_feed(title, fid, href, kind_t, entries):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">'
            f'<id>urn:xteink:{fid}</id><title>{escape(title)}</title><updated>{now_iso()}</updated>'
            '<author><name>Xteink Hub</name></author>'
            f'<link rel="self" href="{href}" type="{kind_t}"/>'
            f'<link rel="start" href="/opds.xml" type="{NAV_T}"/>' + "".join(entries) + '</feed>')


def load_meta(kind):
    items = []
    for m in (HOME / "meta" / kind).glob("*.json"):
        fname = m.name[:-5]
        if (HOME / "files" / kind / fname).exists():
            d = load_json(m, None)
            if d:
                items.append((fname, d))
    return items


def sections():
    out = []
    for kind, label in (("articles", "Articles"), ("books", "Books")):
        items = load_meta(kind)
        for cat, cname in CATS.items():
            sel = [(f, m) for f, m in items if m.get("cat") == cat]
            if cat == "other" and not sel:
                continue
            if kind == "articles":
                sel.sort(key=lambda x: (x[1].get("date", ""), x[1]["title"]), reverse=True)
            else:
                sel.sort(key=lambda x: x[1]["title"].lower())
            out.append((kind, f"{kind}-{cat}", f"{label}: {cname}", sel))
    mine = sorted(load_meta("mine"), key=lambda x: x[1]["title"].lower())
    out.append(("mine", "mine", "My uploads", mine))
    return out


def build_catalog():
    dirs()
    root = []
    for kind, name, title, sel in sections():
        href = f"/opds/{name}.xml"
        entries = [(f'<entry><title>{escape(m["title"])}</title><id>urn:xteink:{kind}:{escape(f)}</id>'
                    f'<updated>{now_iso()}</updated><author><name>{escape(m.get("author", ""))}</name></author>'
                    f'<content type="text">{escape(m.get("summary", ""))}</content>'
                    f'<link rel="http://opds-spec.org/acquisition" href="/files/{kind}/{quote(f)}" '
                    'type="application/epub+zip"/></entry>') for f, m in sel]
        (HOME / "opds" / f"{name}.xml").write_text(atom_feed(title, name, href, ACQ_T, entries), encoding="utf-8")
        root.append(f'<entry><title>{escape(title)}</title><id>urn:xteink:nav:{name}</id>'
                    f'<updated>{now_iso()}</updated><content type="text">{len(sel)} items</content>'
                    f'<link rel="subsection" href="{href}" type="{ACQ_T}"/></entry>')
    (HOME / "opds.xml").write_text(atom_feed("Xteink Hub", "root", "/opds.xml", NAV_T, root), encoding="utf-8")


# ------------------------------------------------------------------ KOReader sync (kosync protocol)
def sync_db():
    return load_json(HOME / "sync.json", {"users": {}, "progress": {}})


def sync_auth(h):
    user, key = h.headers.get("x-auth-user"), h.headers.get("x-auth-key")
    if not user or not key:
        return None
    db = sync_db()
    return user if db["users"].get(user) == key else None


# ------------------------------------------------------------------ network info
def local_ips():
    found = []
    try:  # Android (Chaquopy): ask Java for interface addresses
        from java.net import NetworkInterface
        from java.util import Collections
        for ni in Collections.list(NetworkInterface.getNetworkInterfaces()):
            if not ni.isUp() or ni.isLoopback():
                continue
            for a in Collections.list(ni.getInetAddresses()):
                h = str(a.getHostAddress())
                if ":" not in h:
                    found.append((str(ni.getName()), h))
    except Exception:
        pass
    if not found:
      try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            m = re.search(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)", line)
            if m:
                found.append((m.group(1), m.group(2)))
      except Exception:
        pass
    if not found:
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
            iface = None
            for line in out.splitlines():
                if line and not line[0].isspace():
                    iface = line.split(":")[0].split()[0]
                m = re.search(r"inet (?:addr:)?([\d.]+)", line)
                if m and iface:
                    found.append((iface, m.group(1)))
        except Exception:
            pass
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            found.append(("network", s.getsockname()[0]))
            s.close()
        except Exception:
            pass
    res = []
    for iface, ip in found:
        if ip.startswith("127.") or iface.startswith(("rmnet", "ccmni", "tun", "dummy")):
            continue
        hotspot = iface.startswith(("ap", "swlan", "wlan1", "softap")) or bool(re.match(r"192\.168\.\d+\.1$", ip))
        res.append({"iface": iface, "ip": ip, "hotspot": hotspot})
    if not res:
        res.append({"iface": "hotspot (guess)", "ip": "192.168.43.1", "hotspot": True})
    res.sort(key=lambda r: not r["hotspot"])
    return res


# ------------------------------------------------------------------ HTTP
class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".epub": "application/epub+zip",
                      ".xml": "application/atom+xml;profile=opds-catalog"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HOME), **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # helpers
    def send_json(self, code, obj, ctype="application/json"):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self, limit=200 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def body_json(self):
        try:
            return json.loads(self.body(1024 * 1024) or b"{}")
        except Exception:
            return {}

    def is_admin(self):
        return REMOTE_ADMIN or self.client_address[0] in ("127.0.0.1", "::1")

    # routing
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            b = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/status":
            self.send_json(200, status())
        elif p == "/healthcheck":
            self.send_json(200, {"state": "OK"})
        elif p == "/users/auth":
            if sync_auth(self):
                self.send_json(200, {"authorized": "OK"})
            else:
                self.send_json(401, {"code": 2001, "message": "Unauthorized"})
        elif p.startswith("/syncs/progress/"):
            user = sync_auth(self)
            if not user:
                return self.send_json(401, {"code": 2001, "message": "Unauthorized"})
            doc = unquote(p[len("/syncs/progress/"):])
            self.send_json(200, sync_db()["progress"].get(user, {}).get(doc, {}))
        elif p == "/opds.xml" or p.startswith("/opds/") or p.startswith("/files/"):
            if ".." in p:
                return self.send_error(403)
            super().do_GET()
        else:
            self.send_error(404)

    def do_HEAD(self):
        p = urlparse(self.path).path
        if p == "/opds.xml" or p.startswith(("/opds/", "/files/")):
            return super().do_HEAD()
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == "/users/create":
            d = self.body_json()
            user, key = (d.get("username") or "").strip(), d.get("password") or ""
            if not user or not key:
                return self.send_json(400, {"code": 2003, "message": "Invalid request"})
            with DB_LOCK:
                db = sync_db()
                if user in db["users"]:
                    return self.send_json(402, {"code": 2002, "message": "Username is already registered."})
                if not cfg().get("allow_sync_signup", True):
                    return self.send_json(402, {"code": 2005, "message": "Registration disabled."})
                db["users"][user] = key
                save_json(HOME / "sync.json", db)
            log("Sync: registered user", user)
            return self.send_json(201, {"username": user})
        if not p.startswith("/api/"):
            return self.send_error(404)
        if not self.is_admin():
            return self.send_json(403, {"error": "Use the dashboard on the phone itself."})
        c = cfg()
        if p == "/api/update":
            what = (q.get("what") or ["all"])[0]
            ok = run_update(what if what in ("all", "articles", "books") else "all")
            self.send_json(200, {"started": ok})
        elif p == "/api/feeds/add":
            d = self.body_json()
            if not d.get("name") or not str(d.get("url", "")).startswith("http"):
                return self.send_json(400, {"error": "name and http(s) url needed"})
            c["feeds"].append({"cat": d.get("cat") if d.get("cat") in CATS else "other",
                               "name": d["name"][:60], "url": d["url"]})
            save_json(HOME / "config.json", c)
            self.send_json(200, {"ok": True})
        elif p == "/api/feeds/delete":
            i = int(self.body_json().get("index", -1))
            if 0 <= i < len(c["feeds"]):
                c["feeds"].pop(i)
                save_json(HOME / "config.json", c)
            self.send_json(200, {"ok": True})
        elif p == "/api/settings":
            d = self.body_json()
            for k in ("articles_per_feed", "books_per_topic", "keep_days"):
                if k in d:
                    c[k] = max(1, min(100, int(d[k])))
            if "auto_refresh_hours" in d:
                c["auto_refresh_hours"] = max(0.0, float(d["auto_refresh_hours"]))
            if "allow_sync_signup" in d:
                c["allow_sync_signup"] = bool(d["allow_sync_signup"])
            save_json(HOME / "config.json", c)
            self.send_json(200, {"ok": True})
        elif p == "/api/delete":
            d = self.body_json()
            kind, fname = d.get("kind"), Path(str(d.get("file", ""))).name
            if kind in ("articles", "books", "mine") and fname:
                (HOME / "files" / kind / fname).unlink(missing_ok=True)
                (HOME / "meta" / kind / (fname + ".json")).unlink(missing_ok=True)
                build_catalog()
            self.send_json(200, {"ok": True})
        else:
            self.send_error(404)

    def do_PUT(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == "/syncs/progress":
            user = sync_auth(self)
            if not user:
                return self.send_json(401, {"code": 2001, "message": "Unauthorized"})
            d = self.body_json()
            doc = d.get("document")
            if not doc:
                return self.send_json(400, {"code": 2004, "message": "Field 'document' not provided."})
            ts = int(time.time())
            with DB_LOCK:
                db = sync_db()
                db["progress"].setdefault(user, {})[doc] = {
                    "document": doc, "progress": d.get("progress"), "percentage": d.get("percentage"),
                    "device": d.get("device"), "device_id": d.get("device_id"), "timestamp": ts}
                save_json(HOME / "sync.json", db)
            return self.send_json(200, {"document": doc, "timestamp": ts})
        if p == "/api/upload":
            if not self.is_admin():
                return self.send_json(403, {"error": "Use the dashboard on the phone itself."})
            name = Path((q.get("name") or [""])[0]).name
            if not name.lower().endswith(".epub"):
                return self.send_json(400, {"error": "Only .epub files"})
            fname = re.sub(r"[^\w.\- ]+", "_", name)
            data = self.body()
            if not data.startswith(b"PK"):
                return self.send_json(400, {"error": "Not a valid EPUB"})
            (HOME / "files/mine" / fname).write_bytes(data)
            write_meta("mine", fname, {"title": fname[:-5].replace("_", " "), "author": "", "cat": "mine"})
            build_catalog()
            log("Uploaded", fname)
            return self.send_json(200, {"ok": True})
        self.send_error(404)


def status():
    c = cfg()
    secs = sections()
    db = sync_db()
    return {
        "busy": STATE["busy"], "task": STATE["task"],
        "last_update": STATE["last_update"], "log": list(LOG)[-60:],
        "port": PORT, "ips": local_ips(),
        "sections": [{"name": t, "kind": k, "items": [{"file": f, "title": m["title"]} for f, m in sel]}
                     for k, _, t, sel in secs],
        "feeds": c["feeds"], "settings": {k: c[k] for k in ("articles_per_feed", "books_per_topic", "keep_days",
                                                            "auto_refresh_hours", "allow_sync_signup")},
        "sync_users": list(db["users"].keys()),
    }


DASHBOARD = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xteink Hub</title>
<style>
:root{--bg:#f4f1ea;--card:#fff;--ink:#1d1d1b;--mute:#6b675e;--acc:#1f5f4a;--line:#ddd7ca}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#9b978c;--acc:#5fbf98;--line:#33322e}}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:20px 16px 8px}h1{margin:0;font-size:22px}header p{margin:4px 0 0;color:var(--mute);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:12px;padding:14px}
h2{font-size:15px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
button{font:inherit;border:0;border-radius:10px;padding:12px 14px;background:var(--acc);color:#fff;font-weight:600;width:100%;margin:4px 0}
button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}button.sm{width:auto;padding:6px 10px;font-size:13px;margin:0}
button:disabled{opacity:.5}.row{display:flex;gap:8px}.row>*{flex:1}
code{display:block;font:15px ui-monospace,monospace;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;word-break:break-all;margin:6px 0}
.tag{font-size:12px;color:var(--acc);font-weight:600}.mute{color:var(--mute);font-size:13px}
pre{max-height:220px;overflow:auto;font-size:12px;background:var(--bg);padding:8px;border-radius:8px;margin:8px 0 0;white-space:pre-wrap}
details{border-top:1px solid var(--line);padding:8px 0}summary{cursor:pointer;font-weight:600}
li{margin:4px 0;display:flex;justify-content:space-between;gap:8px;align-items:center}ul{padding:0;list-style:none;margin:6px 0}
input,select{font:inherit;width:100%;padding:9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);margin:3px 0}
label{font-size:13px;color:var(--mute)}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--acc);margin-right:6px}
.busy .dot{background:#d08a2a;animation:p 1s infinite}@keyframes p{50%{opacity:.3}}
</style></head><body>
<header><h1>Xteink Hub</h1><p id="st"><span class="dot"></span>Running</p></header>

<div class="card" id="andr" style="display:none"><h2>App</h2><div class="row">
<button class="ghost" onclick="Android.hotspot()">Hotspot settings</button>
<button class="ghost" onclick="if(confirm('Stop the server and close the app?'))Android.stop()">Stop server</button></div></div>

<div class="card"><h2>1 · Connect the X4</h2>
<div class="mute">Join the X4 to this phone's hotspot (2.4 GHz), then add these in CrossPoint:</div>
<div id="ips"></div></div>

<div class="card"><h2>2 · Get reading material</h2>
<button onclick="upd('all')" id="b1">Update everything</button>
<div class="row"><button class="ghost" onclick="upd('articles')">Articles only</button>
<button class="ghost" onclick="upd('books')">Books only</button></div>
<div class="mute" id="last"></div><pre id="log"></pre></div>

<div class="card"><h2>Add your own EPUB</h2>
<input type="file" id="file" accept=".epub" multiple><button class="ghost" onclick="upload()">Upload to catalog</button></div>

<div class="card"><h2>Library</h2><div id="lib"></div></div>

<div class="card"><h2>Feeds</h2><ul id="feeds"></ul>
<details><summary>Add a feed</summary>
<label>Name</label><input id="fn"><label>RSS URL</label><input id="fu" placeholder="https://...">
<label>Category</label><select id="fc"><option value="scifi">Sci-Fi</option><option value="science">Science</option><option value="other">Other</option></select>
<button onclick="addFeed()">Add feed</button></details></div>

<div class="card"><h2>Settings</h2>
<div class="row"><div><label>Articles per feed</label><input id="s_a" type="number"></div>
<div><label>Books per topic</label><input id="s_b" type="number"></div></div>
<div class="row"><div><label>Keep articles (days)</label><input id="s_k" type="number"></div>
<div><label>Auto-update every (h, 0=off)</label><input id="s_h" type="number" step="0.5"></div></div>
<label><input type="checkbox" id="s_su" style="width:auto"> Allow new sync accounts</label>
<button class="ghost" onclick="saveS()">Save settings</button>
<div class="mute" id="users"></div></div>

<script>
let S={};if(window.Android)document.getElementById('andr').style.display='block';
const $=id=>document.getElementById(id);
const post=(u,d)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})}).then(r=>r.json());
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(full){
  S=await fetch('/api/status').then(r=>r.json());
  document.body.className=S.busy?'busy':'';
  $('st').innerHTML='<span class="dot"></span>'+(S.busy?esc(S.task)+'…':'Running · port '+S.port);
  ['b1'].forEach(i=>$(i).disabled=S.busy);
  $('last').textContent=S.last_update?'Last update: '+new Date(S.last_update*1000).toLocaleString():'Not updated yet — tap “Update everything” while you have internet.';
  const lg=$('log');lg.textContent=S.log.join('\n');lg.scrollTop=1e9;
  $('ips').innerHTML=S.ips.map(i=>`<div style="margin-top:10px"><span class="tag">${i.hotspot?'HOTSPOT':'WI-FI'} · ${esc(i.iface)}</span>
   <div class="mute">OPDS server URL</div><code>http://${i.ip}:${S.port}/opds.xml</code>
   <div class="mute">KOReader Sync server URL</div><code>http://${i.ip}:${S.port}</code></div>`).join('');
  if(!full)return;
  $('lib').innerHTML=S.sections.map(s=>`<details><summary>${esc(s.name)} (${s.items.length})</summary><ul>${
    s.items.map(it=>`<li><span>${esc(it.title)}</span><button class="sm ghost" onclick="del('${s.kind}','${esc(it.file)}')">✕</button></li>`).join('')||'<li class="mute">Empty</li>'}</ul></details>`).join('');
  $('feeds').innerHTML=S.feeds.map((f,i)=>`<li><span>${esc(f.name)} <span class="mute">· ${esc(f.cat)}</span></span><button class="sm ghost" onclick="delFeed(${i})">✕</button></li>`).join('');
  const st=S.settings;$('s_a').value=st.articles_per_feed;$('s_b').value=st.books_per_topic;$('s_k').value=st.keep_days;$('s_h').value=st.auto_refresh_hours;$('s_su').checked=st.allow_sync_signup;
  $('users').textContent='Sync accounts: '+(S.sync_users.join(', ')||'none yet — register from the X4');
}
async function upd(w){await post('/api/update?what='+w);load()}
async function del(k,f){if(confirm('Delete '+f+'?')){await post('/api/delete',{kind:k,file:f});load(1)}}
async function delFeed(i){if(confirm('Remove this feed?')){await post('/api/feeds/delete',{index:i});load(1)}}
async function addFeed(){const r=await post('/api/feeds/add',{name:$('fn').value,url:$('fu').value,cat:$('fc').value});if(r.error)alert(r.error);else{$('fn').value=$('fu').value='';load(1)}}
async function saveS(){await post('/api/settings',{articles_per_feed:$('s_a').value,books_per_topic:$('s_b').value,keep_days:$('s_k').value,auto_refresh_hours:$('s_h').value,allow_sync_signup:$('s_su').checked});alert('Saved');load(1)}
async function upload(){for(const f of $('file').files){const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'PUT',body:f}).then(r=>r.json());if(r.error)alert(f.name+': '+r.error)}$('file').value='';load(1)}
let wasBusy=false;load(1);setInterval(async()=>{await load(0);if(wasBusy&&!S.busy)load(1);wasBusy=S.busy},2500);
</script></body></html>"""


def serve(home):
    """Entry point used by the Android app."""
    global HOME
    HOME = Path(home)
    main()


def main():
    dirs()
    cfg()
    STATE["last_update"] = load_json(HOME / "state.json", {}).get("last_update")
    build_catalog()
    threading.Thread(target=auto_refresher, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        log(f"Port {PORT} is busy (is the Termux version running? run xhub-stop):", e)
        raise
    log(f"Xteink Hub running. Dashboard: http://127.0.0.1:{PORT}/")
    for i in local_ips():
        log(f"  X4 OPDS: http://{i['ip']}:{PORT}/opds.xml   Sync: http://{i['ip']}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
