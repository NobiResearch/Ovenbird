"""cos serve — the local server behind Ovenbird.app (and `cos serve --open` in a browser).

Security model: binds 127.0.0.1 only; every /api call needs the per-launch token (sent
as X-COS-Token, never in a URL or an environment variable); the Host header must be
the loopback address (no DNS rebinding); no CORS headers, so other web pages can't read
or forge requests. The token is printed once, on the READY line, to whoever launched us.

A button the human clicks in the app is their approval: mutating endpoints run the
same `cos … --yes` commands the terminal would, as background jobs, journaled.
"""

import io
import json
import mimetypes
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, chat, core

UI_DIR = core.ROOT / "ui"


def pin_code():
    """Load all of Ovenbird now (0011, part three). Routes import their modules lazily, so a
    checkout that moved while the server ran used to give it new modules beside old ones until
    a restart; now everything it runs is what it started with. -> the module names loaded."""
    import importlib
    import pkgutil
    names = []
    for m in pkgutil.iter_modules(sys.modules[__package__].__path__):
        try:
            importlib.import_module("." + m.name, __package__)
            names.append(m.name)
        except (Exception, SystemExit):
            pass  # it would fail just the same if a route imported it later; never stop the app's server
    return names


UI_FILE_MAX = 5 * 1024 * 1024
UI_TOTAL_MAX = (2000, 50 * 1024 * 1024)  # files, bytes: ui/ is ~5 files and ~150 KB


def read_ui(folder):
    """The desktop's files, read once at start for the same reason: a page reloaded while the
    checkout moved would otherwise get new JavaScript against the old server. Regular files
    only, and a bounded amount: a symlink to /dev/zero or a FIFO in ui/, or ui/ itself made a
    link to the home folder, must not keep the app from its server (it's read before the
    server reports ready, and static files are served without the token)."""
    files, size = {}, 0
    if os.path.islink(str(folder)):
        return files
    for dirpath, _, filenames in os.walk(str(folder)):  # doesn't descend into symlinked folders
        for name in filenames:
            if len(files) >= UI_TOTAL_MAX[0] or size >= UI_TOTAL_MAX[1]:
                return files
            full = os.path.join(dirpath, name)
            try:
                # O_NOFOLLOW: not through a symlink; O_NONBLOCK: a FIFO opens without waiting.
                fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    st = os.fstat(f.fileno())
                    if stat.S_ISREG(st.st_mode) and st.st_size <= UI_FILE_MAX:
                        files[os.path.relpath(full, str(folder))] = body = f.read(UI_FILE_MAX)
                        size += len(body)
            except OSError:
                continue  # an I/O error (or a file iCloud hasn't brought down) is one file less
    return files


class Restarting(Exception):
    """Raised by App.job while the server is on its way out (see r_restart)."""


RESTARTING = "Ovenbird is restarting to update; try again in a moment"
COS = str(core.ROOT / "bin" / "cos")
RESTART = 75  # the exit status that tells Ovenbird.app a restart was planned (EX_TEMPFAIL)
# A package name for `update apply`: never one that starts with a dash and becomes an option.
UPDATE_NAME = re.compile(r"(?!.*\.\.)[A-Za-z0-9@][\w@./+-]*")  # and no `..`: never a local folder


# ---------------------------------------------------------------- jobs

class Job:
    """A `cos` command running in the background, output captured for the UI."""

    def __init__(self, argv, label):
        self.id = uuid.uuid4().hex[:10]
        self.argv, self.label = argv, label
        self.started, self.ended, self.rc = time.time(), None, None
        self.output = []
        try:
            self.proc = subprocess.Popen([sys.executable, COS] + argv, cwd=str(core.ROOT), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", start_new_session=True)
        except OSError as e:
            self.proc, self.rc, self.ended = None, 126, time.time()
            self.output.append("couldn't start: %s" % e)
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.output.append(line.rstrip("\n"))
            del self.output[:-400]
        self.rc = self.proc.wait()
        self.ended = time.time()

    def summary(self):
        return {"id": self.id, "label": self.label, "argv": self.argv, "done": self.ended is not None,
                "rc": self.rc, "output": "\n".join(self.output[-200:]),
                "seconds": round((self.ended or time.time()) - self.started, 1)}


# ---------------------------------------------------------------- helpers

def captured(fn, *args, **kwargs):
    """Run a print-y function and return what it printed."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fn(*args, **kwargs)
        except SystemExit:
            pass
    return buf.getvalue()


_artwork = {}


def artwork_for(track, artist):
    """Album art for the current track via the public iTunes Search API (cached)."""
    from . import music
    key = (track or "", artist or "")
    if not track:
        return None
    if key not in _artwork:
        res = music.catalog_search("%s %s" % (track, artist or ""), limit=3)
        url = None
        for r in res.get("results") or []:
            if r.get("artwork"):
                url = r["artwork"]
                break
        _artwork[key] = url
    return _artwork[key]


def with_artwork(n):
    """The page can only load https art. The player's own musicKit:// links, and the Music
    app's songs (no link at all), get the same looked-up picture, so the cover doesn't flip
    between two images on every play and pause."""
    if n.get("track") and not str(n.get("artwork") or "").startswith("https://"):
        n["artwork"] = artwork_for(n["track"], n.get("artist"))
    return n


def confirm_natively(what, wait=120):
    """Ask in Ovenbird.app's own native dialog, which code in the page can't click (0006, 0011).
    -> True (yes), False (no, or no answer in time) or None (the app couldn't be asked)."""
    import uuid
    if not core.app_running("ClaudeOS"):
        return None
    rid = uuid.uuid4().hex[:12]
    answer = core.home("state", "confirm-%s.json" % what)
    try:
        answer.unlink()
    except OSError:
        pass
    if not core.open_app_url(core.app_url("confirm/" + what, rid=rid), timeout=10).ok:
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        reply = core.load_json(answer)
        if isinstance(reply, dict) and reply.get("rid") == rid:
            return reply.get("approved") is True
    return False


def code_state(app):
    """For the menu bar's Update item: is a restart of the server worth it?"""
    on_disk = core.run(["git", "-C", str(core.ROOT), "rev-parse", "HEAD"], timeout=5).out
    cached = (core.cache_get("catch-up") or {}).get("data") or {}
    autonomy = core.config().get("autonomy")
    auto = isinstance(autonomy, dict) and autonomy.get("catch_up") is True
    waiting = cached.get("behind", 0) if auto and not cached.get("why") else 0
    return {"running": app.head[:7], "on_disk": on_disk[:7], "auto": auto, "waiting": waiting,
            "subjects": cached.get("subjects", [])[:5] if waiting else [],
            "restart": bool(app.managed and app.head and on_disk and (on_disk != app.head or waiting))}


def read_doc(name):
    allowed = {"roadmap": "docs/roadmap.md", "lessons": "docs/lessons.md", "architecture": "docs/architecture.md",
               "changelog": "CHANGELOG.md", "readme": "README.md"}
    if name in allowed:
        path = core.ROOT / allowed[name]
    elif re.match(r"^decisions/\d{4}-[\w-]+$", name or ""):
        path = core.ROOT / "docs" / (name + ".md")
    else:
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    return with_changes(text, core.ROOT / "changes") if name == "changelog" else text


def with_changes(text, folder):
    """The changelog as it will read at the next release: each pull request's entry file in
    changes/ (newest first), under ## Unreleased."""
    entries = sorted((p for p in folder.glob("[0-9]*.md")), key=lambda p: p.name, reverse=True) if folder.is_dir() else []
    pending = "\n".join(p.read_text().strip() + "\n" for p in entries)
    if not pending:
        return text
    head = "## Unreleased\n"
    if head not in text:
        return text.rstrip() + "\n\n" + head + "\n" + pending
    return text.replace(head, head + "\n" + pending, 1)


def decisions():
    d = core.ROOT / "docs" / "decisions"
    out = []
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        first = p.read_text().splitlines()[0].lstrip("# ").strip() if p.stat().st_size else p.stem
        out.append({"name": "decisions/" + p.stem, "title": first})
    return out


def latest_run(name):
    runs = sorted(core.home("runs", name).glob("*.md")) if core.home("runs", name).is_dir() else []
    if not runs:
        return None
    text, details = split_run(runs[-1].read_text())
    return {"file": runs[-1].name, "ts": runs[-1].stat().st_mtime, "text": text, "details": details}


def split_run(text):
    """-> (the document, what came before it). A run narrates as it goes ("Steward is in…")
    before its first `# ` heading; older runs also end in a stderr section."""
    text = text.split("\n\n---\nstderr:\n", 1)[0]
    m = re.search(r"^# ", text, re.M)
    if not m or m.start() == 0:
        return text, ""
    return text[m.start():], text[:m.start()].strip()


# ---------------------------------------------------------------- the handler

class App:
    def __init__(self, token, demo=False, head=None):
        self.token = token
        self.demo = demo
        if demo:
            from . import demo as demo_mod
            self.chats = chat.Registry(factory=demo_mod.DemoSession)
        else:
            self.chats = chat.Registry()
        self.jobs = {}
        # Set, under the lock, once a restart is certain: no job may start after that (r_restart).
        self.restart_lock = threading.Lock()
        self.restarting = False
        self.port = None
        # The commit this server's code came from: once the checkout moves, it's out of date
        # (serve() pins its modules and desktop files to this commit; 0011, part three).
        self.head = head or core.run(["git", "-C", str(core.ROOT), "rev-parse", "HEAD"], timeout=5).out
        self.ui = read_ui(UI_DIR)
        self.managed = False  # started by Ovenbird.app, which restarts it (see r_restart)


    def job(self, argv, label):
        with self.restart_lock:
            if self.restarting:
                raise Restarting()
            j = Job(argv, label)
            self.jobs[j.id] = j
        core.journal("approval", "approved in the app: cos %s" % " ".join(argv), via="app")
        return j.summary()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudeOS/" + __version__
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------------------------------------------------- transport

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

        def _error(self, code, msg):
            self._json({"error": msg}, code)

        def _host_ok(self):
            host = self.headers.get("Host", "")
            return host in ("127.0.0.1:%d" % app.port, "localhost:%d" % app.port)

        def _authed(self):
            return secrets.compare_digest(self.headers.get("X-COS-Token", ""), app.token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return None
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return None

        # ---------------------------------------------------- dispatch

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method):
            if not self._host_ok():
                return self._error(421, "wrong host")
            url = urllib.parse.urlsplit(self.path)
            path, query = url.path, dict(urllib.parse.parse_qsl(url.query))
            if not path.startswith("/api/"):
                return self._static(path) if method == "GET" else self._error(405, "no")
            if not self._authed():
                return self._error(401, "missing or wrong token")
            body = self._body() if method == "POST" else {}
            if body is None:
                return self._error(400, "bad json")
            if app.demo and self._demo(method, path, body):
                return
            if app.restarting and method == "POST":
                return self._error(503, RESTARTING)
            try:
                route = ROUTES.get((method, path))
                if route:
                    return route(self, query, body)
                m = re.match(r"^/api/chat/([0-9a-f]{12})/(events|send|permission|interrupt|close)$", path)
                if m:
                    return self._chat(m.group(1), m.group(2), method, query, body)
                m = re.match(r"^/api/jobs/([0-9a-f]{10})$", path)
                if m and method == "GET":
                    j = app.jobs.get(m.group(1))
                    return self._json(j.summary()) if j else self._error(404, "no such job")
                return self._error(404, "no such endpoint")
            except Restarting:
                return self._error(503, RESTARTING)
            except Exception as e:  # keep the server alive; tell the UI what broke
                return self._error(500, "%s: %s" % (type(e).__name__, e))

        def _demo(self, method, path, body):
            """Demo mode: answer from fixtures and never touch the machine. Chat (scripted)
            and the public docs fall through to the normal handlers."""
            from . import demo
            if path.startswith("/api/chat") or path.startswith("/api/doc"):
                return False
            if method == "GET":
                if path == "/api/board":
                    self._json({"latest": None, "decisions": decisions()})
                elif path.startswith("/api/jobs/"):
                    self._json(demo.fake_job("demo"))
                else:
                    fixture = demo.handle_get(path)
                    self._json(fixture if fixture is not None else {})
                return True
            if path.startswith("/api/music/"):
                self._json(dict(demo.MUSIC_NOW, ok=True, now=demo.MUSIC_NOW, source="library"))
            elif path.startswith("/api/memory"):
                self._json(demo.GETS["/api/memory"])
            elif path.startswith("/api/weather"):
                self._json({"name": demo.WEATHER["place"], "source": "city"})
            elif path.startswith("/api/settings"):
                self._json(demo.SETTINGS["music"])
            elif path.startswith("/api/agenda"):
                self._json(demo.AGENDA)
            elif path.endswith(("/apply", "/merge", "/discard", "/install", "/uninstall")):
                self._json(demo.fake_job("demo"))
            else:
                self._json({"ok": True, "demo": True, "id": 0, "text": body.get("text"), "marked": 0})
            return True

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("..") or os.path.isabs(rel):
                return self._error(404, "no")
            body = app.ui.get(rel)
            if body is None:
                return self._error(404, "no such file")
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            csp = ("default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
                   "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self._send(200, body, ctype, {"Content-Security-Policy": csp})

        # ---------------------------------------------------- chat

        def _chat(self, sid, action, method, query, body):
            s = app.chats.get(sid)
            if not s:
                return self._error(404, "no such chat")
            if action == "events" and method == "GET":
                return self._stream(s, int(query.get("after") or 0))
            if method != "POST":
                return self._error(405, "use POST")
            if action == "send":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._error(400, "empty message")
                return self._json({"ok": s.send(text), "chat": s.summary()})
            if action == "permission":
                ok = s.answer(body.get("request_id"), bool(body.get("allow")), body.get("message"))
                return self._json({"ok": ok})
            if action == "interrupt":
                return self._json({"ok": s.interrupt()})
            if action == "close":
                return self._json({"ok": app.chats.close(sid)})

        def _stream(self, s, after):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    events = s.wait(after, timeout=15)
                    if events:
                        for e in events:
                            self.wfile.write(("data: %s\n\n" % json.dumps(e, ensure_ascii=False, default=str)).encode())
                            after = e["seq"]
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if s.state == "closed" and not s.wait(after, timeout=0):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------------------------------------------------- routes

    def r_boot(h, q, b):
        from . import shell
        h._json({"version": __version__, "lines": captured(shell.boot).splitlines()})

    def r_status(h, q, b):
        from . import status, updates
        s = status.collect()
        core.cache_put("status", s)
        if app.managed:  # beside the poll: a slow network doesn't hold it up
            threading.Thread(target=updates.fold_app, daemon=True).start()
        h._json(dict(s, claude_os=code_state(app), new_here=core.new_here(), downloaded=core.downloaded()))

    def r_restart(h, q, b):
        """Restart only the server (music plays on in the app): exit 75 tells Ovenbird.app
        it was planned. With Keep Ovenbird up to date on, the new server catches up first."""
        if not app.managed:
            return h._error(400, "only Ovenbird.app restarts its server; in a browser, restart `cos serve`")
        # A job's output comes through this process: a restart would cut a `clean apply` or an
        # `update apply` off halfway.
        running = [j for j in list(app.jobs.values()) if j.ended is None]
        if running:
            return h._error(409, "“%s” is still running; Update when it's done" % running[0].label)
        h._json({"ok": True})

        def later():
            time.sleep(0.3)
            with app.restart_lock:
                if any(j.ended is None for j in list(app.jobs.values())):
                    return  # one started in the meantime: the Update item stays for later
                app.restarting = True  # from here on, no job and no POST starts (App.job, dispatch)
            app.chats.close_all()
            core.journal("action", "Ovenbird's server restarted to update", via="app")
            os._exit(RESTART)
        threading.Thread(target=later, daemon=True).start()

    def r_clean(h, q, b):
        from . import clean
        cached = core.cache_get("clean")
        if q.get("scan") or not cached:
            data = clean.scan(deep=bool(q.get("deep")))
            return h._json({"ts": time.time(), "data": data})
        h._json(cached)

    def r_clean_apply(h, q, b):
        from . import clean
        ids = [i for i in b.get("ids") or [] if i in clean.BY_ID]
        if not ids:
            return h._error(400, "no valid targets")
        h._json(app.job(["clean", "apply"] + ids + ["--yes"], "Reclaim " + ", ".join(ids)))

    def r_updates(h, q, b):
        from . import updates
        cached = core.cache_get("updates")
        if q.get("check") or not cached:
            data = updates.check(fetch=bool(q.get("fetch")))
            return h._json({"ts": time.time(), "data": data})
        if app.managed:
            updates.fold_app()
            cached = core.cache_get("updates") or cached
        h._json(cached)

    def r_updates_apply(h, q, b):
        source = b.get("source")
        if source not in ("brew", "npm", "mas", "rustup", "macos"):
            return h._error(400, "unknown source")
        names = [n for n in b.get("names") or [] if isinstance(n, str) and UPDATE_NAME.fullmatch(n)]
        argv = ["update", "apply", source] + names + ([] if source == "macos" else ["--yes"])
        h._json(app.job(argv, "Update %s %s" % (source, " ".join(names) or "(all)")))

    def r_settings(h, q, b):
        from . import music, weather
        cfg = core.config()
        st = music.player_state() or {}
        place = weather.location() or {}
        from . import cal
        cs = cal.state() or {}
        ns = core.load_json(core.home("state", "notify.json"), {}) or {}
        h._json({"notify": {"native": bool(cfg["notify"].get("native")),
                            "authorization": ns.get("authorization", "not determined")},
                 "calendar": cs.get("calendar", "not connected"), "calendars": cs.get("calendars"),
                 "music": {"apple_music": bool(cfg["music"].get("apple_music")),
                           "authorization": st.get("authorization", "not asked yet"), "error": st.get("error"),
                           "shortcut": music.shortcut_installed()},
                 "weather": {"name": place.get("name"), "source": place.get("source")},
                 "catch_up": bool(cfg["autonomy"].get("catch_up")),
                 "downloaded": core.downloaded()})

    def r_settings_music(h, q, b):
        from . import music
        on = bool(b.get("apple_music"))
        core.config_set("music.apple_music", on)
        core.journal("action", "Apple Music in Ovenbird turned %s" % ("on" if on else "off"), via="app")
        if on:
            res = music.player_command("authorize", wait=60)  # macOS shows its own prompt; the human decides
            st = res.get("state") or {}
            return h._json({"apple_music": True, "authorization": st.get("authorization"), "error": res.get("error")})
        core.run(["open", music.MEDIA_PANE], timeout=10)  # off here doesn't revoke it in macOS; show where
        h._json({"apple_music": False})

    def r_settings_catch_up(h, q, b):
        on = bool(b.get("on"))
        if on:
            answer = confirm_natively("catch-up")  # the page can call this route, but can't click the dialog
            if answer is not True:
                core.journal("action", "Keep Ovenbird up to date stayed off (%s)" % (
                    "declined in the app window" if answer is False else "couldn't ask in Ovenbird.app"), via="app")
                return h._json({"catch_up": False, "confirmed": answer,
                                "error": None if answer is False else "It can only be turned on in Ovenbird.app"})
            core.config_set("autonomy.catch_up_url",
                            core.run(["git", "-C", str(core.ROOT), "config", "--get", "remote.origin.url"], timeout=5).out)
        core.config_set("autonomy.catch_up", on)
        core.journal("action", "Keep Ovenbird up to date turned %s%s" % (
            "on" if on else "off", " (confirmed in the app window)" if on else ""), via="app")
        h._json({"catch_up": on})

    def r_settings_notify(h, q, b):
        on = bool(b.get("native"))
        core.config_set("notify.native", on)
        core.journal("action", "Ovenbird notifications turned %s" % ("on" if on else "off"), via="app")
        if not on:
            return h._json({"native": False})
        before = (core.load_json(core.home("state", "notify.json"), {}) or {}).get("ts", 0)
        core.open_app_url(core.app_url("notify/authorize"), timeout=10)  # macOS asks; the human decides
        status = {}
        for _ in range(120):
            time.sleep(0.5)
            status = core.load_json(core.home("state", "notify.json"), {}) or {}
            if status.get("ts", 0) > before:
                break
        h._json({"native": True, "authorization": status.get("authorization", "not determined")})

    def r_weather(h, q, b):
        from . import weather
        try:
            h._json(weather.forecast(force=bool(q.get("refresh"))))
        except Exception as e:
            h._json({"configured": True, "error": str(e)})

    def r_weather_city(h, q, b):
        from . import weather
        query = (b.get("query") or "").strip()
        if not query:
            return h._error(400, "type a city")
        place = weather.set_city(query)
        if not place:
            return h._error(404, "couldn't find %s" % query)
        h._json({"name": place["name"], "source": "city"})

    def r_weather_coords(h, q, b):
        from . import weather
        try:
            place = weather.set_coords(b.get("lat"), b.get("lon"))
        except (TypeError, ValueError) as e:
            return h._error(400, str(e))
        h._json({"name": place["name"], "source": "device"})

    def r_weather_clear(h, q, b):
        from . import weather
        weather.clear()
        h._json({"name": None})

    def r_agenda(h, q, b):
        from . import cal
        h._json(cal.day_view(cal.agenda(refresh=bool(q.get("refresh")))))

    def r_agenda_connect(h, q, b):
        from . import cal
        core.journal("action", "calendar connect requested", via="app")
        h._json(cal.day_view(cal.connect() or {}))

    def r_startup(h, q, b):
        from . import startup
        cached = core.cache_get("startup", max_age=600)
        d = cached["data"] if cached else core.cache_put("startup", startup.audit())["data"]
        h._json({"count": len(d.get("items", [])), "enabled": d.get("enabled", 0), "flagged": d.get("flagged", 0),
                 "error": d.get("error")})

    def r_trends(h, q, b):
        from . import status
        rows = status.trends()
        h._json({"rows": rows[-90:], "disk_per_week": status.disk_trend(rows)})

    def r_music_now(h, q, b):
        """The desktop's poll: nobody clicked anything, so it leaves Music.app alone until someone
        has (music.asked)."""
        from . import music
        h._json(with_artwork(music.now(ask_music=music.asked())))

    def r_music_search(h, q, b):
        from . import music
        query = (q.get("q") or "").strip()
        if not query:
            return h._json({"library": [], "catalog": []})
        library = music.library_search(query, limit=8)
        music._note_asked()
        h._json({"library": library, "catalog": music.catalog_search(query, limit=10).get("results", [])})

    def r_music_playlists(h, q, b):
        """Asked when the Music window opens, which can be the desktop reopening it: see r_music_now."""
        from . import music
        h._json(music.playlists() if music.asked() else [])

    def r_music_play(h, q, b):
        from . import music
        if b.get("radio"):
            res = music.radio(b.get("q") or None, mine=b.get("mine") is True)
        elif b.get("playlist"):
            res = music.play_playlist(b["playlist"], shuffle=True if b.get("shuffle") else None)
        elif b.get("id"):
            res = music.play_library({"id": b["id"], "name": b.get("name")})
            res["source"] = "library"
        elif b.get("url"):
            res = music.play_catalog({"url": b["url"], "name": b.get("name"), "artist": b.get("artist"),
                                      "album": b.get("album"), "track_number": b.get("track_number")})
            res["source"] = "catalog"
        else:
            res = music.play(b.get("q") or "", source=b.get("source") or "auto")
        music._note_asked()
        if res.get("ok"):
            n = res.get("now") or {}
            song = "%s — %s" % (n.get("track") or b.get("name"), n.get("artist") or b.get("artist"))
            event = "radio" if b.get("radio") else "play"
            # The catalog id and the label come from what the player did, never from the request.
            music.journal_event(event, ("radio from %s" if event == "radio" else "played %s") % song,
                                catalog_id=res.get("catalog_id"),
                                label="%s — %s" % (n["track"], n.get("artist") or "?") if n.get("track") else None,
                                via="app", source=res.get("source"), station=res.get("station"))
        h._json(res)

    def r_music_station(h, q, b):
        """What Today's Radio chip says it starts: {"label"}, or {} when there's nothing to
        start from or the player is off (then the chip stays hidden)."""
        from . import music
        s = music.station_seed() if music.player_enabled() else None
        h._json({"label": s["label"]} if s else {})

    def r_net(h, q, b):
        """Vitals' Network card, on a click only: is it the Wi-Fi, the router or the internet?
        The same probes as `cos net` (0010), a few seconds, nothing kept or journaled."""
        from . import net
        h._json(net.summary(net.check()))

    def r_music_control(h, q, b):
        from . import music
        cmd = b.get("cmd")
        if cmd in ("pause", "resume", "toggle", "next", "prev", "stop"):
            captured(music.transport, cmd)
        elif cmd == "volume":
            captured(music.volume, int(b.get("level") or 0))
        elif cmd == "like":
            captured(music.like)
        else:
            return h._error(400, "unknown control")
        n = music.now()  # first: someone slow on macOS's dialog times the click out, and this answers
        music._note_asked()
        h._json(with_artwork(n))

    def r_feeds(h, q, b):
        from . import feeds
        store = feeds.refresh(force=bool(q.get("refresh")))
        items = feeds.unread(store, category=q.get("category") or None, include_read=bool(q.get("all")))[:150]
        cats = sorted({f["category"] for f in store.get("feeds", {}).values()})
        h._json({"items": items, "categories": cats, "fetched": store.get("ts")})

    def r_feeds_read(h, q, b):
        from . import feeds
        keys = b.get("keys")
        if b.get("all"):
            keys = [i["key"] for i in feeds.unread(feeds.refresh(force=False))]
        feeds.mark_read(keys or [])
        h._json({"ok": True, "marked": len(keys or [])})

    def r_friction(h, q, b):
        from . import mem
        h._json([dict(f, lead=mem.lead(f)) for f in mem.frictions()])

    def r_friction_add(h, q, b):
        from . import mem
        text = (b.get("text") or "").strip()
        if not text:
            return h._error(400, "empty")
        h._json(mem.add_friction(text, area=b.get("area") or None))

    def r_friction_close(h, q, b):
        from . import mem
        f = mem.close_friction(int(b.get("id") or 0), note=b.get("note"))
        h._json(f or {"error": "no such friction"}, 200 if f else 404)

    def r_memory(h, q, b):
        from . import mem
        text = mem.memory_text()
        h._json({"text": text, "facts": [l[2:] for l in text.splitlines() if l.startswith("- ")]})

    def r_remember(h, q, b):
        from . import mem
        fact = (b.get("fact") or "").strip()
        if not fact:
            return h._error(400, "empty")
        mem.remember(fact)
        r_memory(h, q, b)

    def r_forget(h, q, b):
        from . import mem
        mem.forget(b.get("pattern") or "\0")
        r_memory(h, q, b)

    def r_log(h, q, b):
        h._json(core.read_journal(days=int(q.get("days") or 7), kind=q.get("kind") or None)[-500:])

    def r_evolve(h, q, b):
        from . import evolve
        h._json(evolve.status_rows())

    def r_evolve_diff(h, q, b):
        from . import evolve
        br = q.get("branch") or ""
        if not re.match(r"^evolve/[\w.-]+$", br):
            return h._error(400, "not an evolve branch")
        base = evolve.main_branch()
        h._json({"log": evolve.git("log", "--stat", "%s..%s" % (base, br)).out,
                 "diff": evolve.git("diff", "%s...%s" % (base, br)).out[:200000]})

    def r_evolve_act(action):
        def route(h, q, b):
            br = b.get("branch") or ""
            if not re.match(r"^evolve/[\w.-]+$", br):
                return h._error(400, "not an evolve branch")
            # The Merge dialog names a branch's safety files; the human's OK is their review (0013),
            # of the commit the dialog showed: a branch that moved since isn't merged on it.
            extra = []
            if action == "merge":
                shown = b.get("tip")
                if shown is not None and not (isinstance(shown, str) and re.fullmatch(r"[0-9a-f]{40}", shown)):
                    return h._error(400, "tip must be a full commit id")
                if b.get("reviewed") is True and not shown:
                    return h._error(400, "a review is of a commit: send the tip that was shown")
                extra = (["--reviewed"] if b.get("reviewed") is True else []) + (["--tip", shown] if shown else [])
            h._json(app.job(["evolve", action, br, "--yes"] + extra, "%s %s" % (action.capitalize(), br)))
        return route

    def r_daemons(h, q, b):
        from . import daemon
        h._json(daemon.status())

    def r_daemon_act(action):
        def route(h, q, b):
            from . import daemon
            name = b.get("name")
            if name not in daemon.definitions():
                return h._error(400, "no such daemon")
            h._json(app.job(["daemon", action, name], "%s %s" % (action.capitalize(), name)))
        return route

    def r_brief(h, q, b):
        h._json(latest_run("morning") or {})

    def r_board(h, q, b):
        from . import daemon
        h._json({"latest": latest_run("board"), "decisions": decisions(), "live": daemon.live("board")})

    def r_doc(h, q, b):
        text = read_doc(q.get("name"))
        h._json({"name": q.get("name"), "text": text} if text is not None else {"error": "no such doc"},
                200 if text is not None else 404)

    def r_chat_new(h, q, b):
        s = app.chats.create()
        if (b.get("text") or "").strip():
            s.send(b["text"].strip())
        h._json(s.summary())

    def r_chats(h, q, b):
        h._json([s.summary() for s in app.chats.sessions.values()])

    def r_open(h, q, b):
        url = b.get("url") or ""
        if not re.match(r"^https?://", url):
            return h._error(400, "only http(s) links")
        core.run(["open", url], timeout=10)
        h._json({"ok": True})

    ROUTES = {
        ("GET", "/api/boot"): r_boot,
        ("GET", "/api/status"): r_status,
        ("GET", "/api/clean"): r_clean,
        ("POST", "/api/clean/apply"): r_clean_apply,
        ("GET", "/api/updates"): r_updates,
        ("POST", "/api/updates/apply"): r_updates_apply,
        ("GET", "/api/trends"): r_trends,
        ("GET", "/api/settings"): r_settings,
        ("GET", "/api/agenda"): r_agenda,
        ("GET", "/api/startup"): r_startup,
        ("POST", "/api/agenda/connect"): r_agenda_connect,
        ("POST", "/api/settings/music"): r_settings_music,
        ("POST", "/api/settings/notify"): r_settings_notify,
        ("POST", "/api/settings/catch-up"): r_settings_catch_up,
        ("POST", "/api/restart"): r_restart,
        ("GET", "/api/weather"): r_weather,
        ("POST", "/api/weather/city"): r_weather_city,
        ("POST", "/api/weather/coords"): r_weather_coords,
        ("POST", "/api/weather/clear"): r_weather_clear,
        ("GET", "/api/music/now"): r_music_now,
        ("GET", "/api/music/station"): r_music_station,
        ("GET", "/api/net"): r_net,
        ("GET", "/api/music/search"): r_music_search,
        ("GET", "/api/music/playlists"): r_music_playlists,
        ("POST", "/api/music/play"): r_music_play,
        ("POST", "/api/music/control"): r_music_control,
        ("GET", "/api/feeds"): r_feeds,
        ("POST", "/api/feeds/read"): r_feeds_read,
        ("GET", "/api/friction"): r_friction,
        ("POST", "/api/friction"): r_friction_add,
        ("POST", "/api/friction/close"): r_friction_close,
        ("GET", "/api/memory"): r_memory,
        ("POST", "/api/memory/remember"): r_remember,
        ("POST", "/api/memory/forget"): r_forget,
        ("GET", "/api/log"): r_log,
        ("GET", "/api/evolve"): r_evolve,
        ("GET", "/api/evolve/diff"): r_evolve_diff,
        ("POST", "/api/evolve/merge"): r_evolve_act("merge"),
        ("POST", "/api/evolve/discard"): r_evolve_act("discard"),
        ("GET", "/api/daemons"): r_daemons,
        ("POST", "/api/daemons/install"): r_daemon_act("install"),
        ("POST", "/api/daemons/uninstall"): r_daemon_act("uninstall"),
        ("GET", "/api/brief"): r_brief,
        ("GET", "/api/board"): r_board,
        ("GET", "/api/doc"): r_doc,
        ("GET", "/api/chats"): r_chats,
        ("POST", "/api/chat"): r_chat_new,
        ("POST", "/api/open"): r_open,
    }
    return Handler


# ---------------------------------------------------------------- entry

PREFERRED_PORT = 7357  # a stable origin keeps the desktop's window layout across launches


def bind(port, handler):
    if port == 0:
        try:
            return ThreadingHTTPServer(("127.0.0.1", PREFERRED_PORT), handler)
        except OSError:
            pass  # taken: any free port will do
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(port=0, open_browser=False, exit_on_stdin_eof=False, token=None, demo=False, head=None):
    pin_code()
    app = App(token or secrets.token_urlsafe(24), demo=demo, head=head)
    httpd = bind(port, make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    app.managed = exit_on_stdin_eof
    if exit_on_stdin_eof:  # only the app passes it
        from . import music
        os.environ[music.UNDER_THE_APP] = "1"
    print("COS_SERVE_READY port=%d token=%s" % (app.port, app.token), flush=True)
    if exit_on_stdin_eof:
        def watch():
            try:
                while sys.stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            app.chats.close_all()
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    if open_browser:
        webbrowser.open("http://127.0.0.1:%d/#token=%s" % (app.port, app.token))
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.chats.close_all()
    return 0


def main(args):
    return serve(port=args.port, open_browser=args.open, exit_on_stdin_eof=args.exit_on_stdin_eof, demo=args.demo,
                 head=getattr(args, "head", None))
