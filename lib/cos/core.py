"""Shared plumbing for every syscall: paths, processes, journal, config, cache.

Everything private lives under CLAUDE_OS_HOME (default ~/.claude-os), never in
the repository. The repository is public; this module is the boundary.
"""

import datetime as _dt
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def home(*parts):
    """Private state directory (created on demand)."""
    base = Path(os.environ.get("CLAUDE_OS_HOME", "~/.claude-os")).expanduser()
    path = base.joinpath(*parts)
    target = path if not path.suffix else path.parent
    if not target.is_dir():
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only contexts (sandboxed evolve runs) still get a path back
    return path


# ---------------------------------------------------------------- processes

class Result:
    __slots__ = ("rc", "out", "err")

    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

    @property
    def ok(self):
        return self.rc == 0

    def __repr__(self):
        return "Result(rc=%r, out=%r, err=%r)" % (self.rc, self.out[:80], self.err[:80])


def run(cmd, timeout=30, input=None, env=None, cwd=None):
    """Run a command and capture its output. Never raises for missing or unrunnable
    binaries, undecodable output, or timeouts; on timeout or Ctrl-C the whole process
    group is killed so no grandchildren are left behind."""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=full_env, cwd=cwd, start_new_session=True,
        )
    except FileNotFoundError:
        return Result(127, "", "command not found: %s" % cmd[0])
    except OSError as e:
        return Result(126, "", "cannot run %s: %s" % (cmd[0], e))
    try:
        out, err = p.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _killpg(p)
        out, err = _drain(p)
        return Result(124, out.strip(), ("timed out after %ss: %s\n%s" % (timeout, " ".join(cmd), err)).strip())
    except KeyboardInterrupt:
        _killpg(p)
        raise
    return Result(p.returncode, out.strip(), err.strip())


def stream(cmd, on_line, timeout=30, input=None, env=None, cwd=None):
    """Like run(), but each stdout line goes to on_line as it arrives (and isn't kept). Same
    promises: never raises for a missing binary or a timeout, and the whole process group
    dies on timeout, on Ctrl-C, on any error here and when the command ends. The deadline
    doesn't wait for output, stderr drains on its own thread (the last lines are kept), and
    an on_line that fails loses that line, not the run."""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        p = subprocess.Popen(
            cmd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=full_env, cwd=cwd, start_new_session=True,
        )
    except FileNotFoundError:
        return Result(127, "", "command not found: %s" % cmd[0])
    except OSError as e:
        return Result(126, "", "cannot run %s: %s" % (cmd[0], e))
    tail = deque(maxlen=40)
    expired = threading.Event()

    def read_stderr():
        for line in p.stderr:
            tail.append(line.rstrip())

    def expire():
        expired.set()
        _killpg(p)

    reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    finished = False
    try:
        if input is not None:
            try:
                p.stdin.write(input)
                p.stdin.close()
            except OSError:
                pass  # it exited (or was killed) before reading its input
        for line in p.stdout:
            try:
                on_line(line)
            except Exception:
                pass
        finished = True
    finally:
        timer.cancel()
        if finished:  # give it a moment to exit by itself after closing its output
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _killpg(p)  # also takes any grandchildren still holding on
        p.wait()
        reader.join(timeout=2)
    err = "\n".join(tail).strip()
    if expired.is_set():
        return Result(124, "", ("timed out after %ss\n%s" % (timeout, err)).strip())
    return Result(p.returncode, "", err)


def _killpg(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _drain(p):
    try:
        out, err = p.communicate(timeout=5)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        return "", ""


# Deny by default: what model-written code may touch is listed, not what it mayn't. It reads
# the system, the toolchains (Python, Xcode's, Homebrew) and its own two folders, writes only
# those, and has no network: only Unix sockets inside its own temp folder, where the tests'
# servers listen. Nothing on localhost (a dev server, a database, a browser bridge, a proxy)
# and nothing on the local network is reachable. No Apple Events, no
# LaunchServices (`open` a URL), no pasteboard, no DNS, nothing of the human's home folder,
# not even which files are in it. bsd.sb brings the system's own plumbing: logging,
# preferences, user lookups, and stat() anywhere (for symlinks), which the home rule narrows.
# Later rules win.
SANDBOX_PROFILE = """(version 1)
(deny default)
(import "bsd.sb")
(allow process-fork)
(allow process-exec)
(allow signal (target same-sandbox))
(allow sysctl-read)
(allow file-read* (literal "/") %(readable)s)
(allow file-write*
  (subpath %(cwd)s) (subpath %(tmp)s)
  (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/fd/"))
(allow file-ioctl (literal "/dev/null") (literal "/dev/tty"))
(allow network-bind network-inbound (local unix-socket (subpath %(tmp)s)))
(allow network-outbound (remote unix-socket (subpath %(tmp)s)))
(deny file-read-metadata (subpath %(home)s))
(allow file-read-metadata %(way_in)s)
(deny file-read* file-read-metadata %(private)s)
"""
SANDBOX_READ = ("/System", "/usr", "/bin", "/sbin", "/dev", "/private/etc", "/private/var/db/timezone",
                "/private/var/db/dyld", "/private/var/select", "/Library/Frameworks/Python.framework",
                "/Library/Developer/CommandLineTools", "/Applications/Xcode.app", "/opt/homebrew")

# Never readable by model-written code, even if its folder were inside the home folder:
# credentials and the human's private state: keys and logins, Claude Code's settings and
# account, and the histories shells and tools keep (where they keep them by default).
PRIVATE_DIRS = (".ssh", ".claude-os", ".claude", ".claude.json", ".claude.json.backup", ".aws", ".gnupg",
                ".config/gh", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".config/git/credentials",
                ".docker", ".kube", ".config/gcloud", ".azure", ".cargo/credentials.toml", ".cargo/credentials", ".gem/credentials",
                ".password-store", "Library/Keychains", "Library/Application Support/com.apple.TCC",
                ".zsh_history", ".zsh_sessions", ".bash_history", ".bash_sessions", ".local/share/fish",
                ".python_history", ".node_repl_history", ".psql_history", ".mysql_history",
                ".sqlite_history", ".lesshst", ".viminfo")
# The PATH entries the sandbox can run from; the rest of the human's PATH stays outside.
SANDBOX_BIN = ("/usr/", "/bin", "/sbin", "/opt/homebrew/", "/Library/Frameworks/Python.framework/",
               "/Library/Developer/CommandLineTools/", "/Applications/Xcode.app/")


def _other_xcode(developer, applications="/Applications"):
    """The selected developer folder's Xcode, when it's in /Applications under another name
    (Xcode-beta.app, CI's Xcode_26.6.app, or Xcode.app as a link to Xcode-26.app): one more
    folder the sandbox may read and run from. () for anything else, and for Xcode.app and the
    command line tools, which are always in. Only a real Xcode right in /Applications counts,
    after links: the sandbox sees real paths, and a link out to the home folder stays out."""
    import plistlib
    real = os.path.realpath(developer) if developer else ""
    app = os.path.dirname(os.path.dirname(real))
    if (not app.endswith(".app") or os.path.dirname(app) != os.path.realpath(applications)
            or real != os.path.join(app, "Contents", "Developer") or (app + "/").startswith(SANDBOX_BIN)):
        return ()
    info = os.path.join(app, "Contents", "Info.plist")
    if not os.path.isfile(info):  # a FIFO there would hang the read
        return ()
    try:
        with open(info, "rb") as f:
            bundle = plistlib.load(f).get("CFBundleIdentifier")
    except Exception:  # unreadable or not a plist: not an Xcode we can vouch for
        return ()
    return (app,) if bundle == "com.apple.dt.Xcode" else ()


def _sandbox_path(path, developer, xcode=()):
    """The human's PATH, cut to the toolchains, with the developer folder's real tools ahead of
    /usr/bin. /usr/bin/git, python3, strings and nm are xcrun shims, and xcrun keeps a cache in
    the shared per-user temp folder; the sandbox mustn't write there. The command line tools
    keep all four in usr/bin; Xcode keeps strings and nm in its default toolchain. `xcode` is
    _other_xcode's folder, if any."""
    allowed = SANDBOX_BIN + tuple(x + "/" for x in xcode)
    keep = [p for p in path.split(":") if p.startswith(allowed)] or ["/usr/bin", "/bin"]
    folders = [os.path.join(developer, "usr", "bin"),
               os.path.join(developer, "Toolchains", "XcodeDefault.xctoolchain", "usr", "bin")] if developer else []
    tools = [t for t in folders if t.startswith(allowed) and os.path.isdir(t)]
    rest = [p for p in keep if p not in tools]
    at = rest.index("/usr/bin") if "/usr/bin" in rest else 0
    return ":".join(rest[:at] + tools + rest[at:])


def run_sandboxed(cmd, cwd, timeout=600):
    """Run untrusted code (e.g. tests an evolve run wrote) under the deny-by-default profile,
    with a throwaway HOME and only the environment set here: nothing the human's shell
    exports (tokens, keys) reaches it."""
    import tempfile
    real = os.path.realpath(cwd)
    home_dir = os.path.realpath(os.path.expanduser("~"))
    private = " ".join("(subpath %s)" % json.dumps(os.path.join(home_dir, p)) for p in PRIVATE_DIRS)
    developer = run(["xcode-select", "-p"], timeout=5).out.strip()
    developer = os.path.realpath(developer) if developer else ""  # the sandbox sees real paths
    xcode = _other_xcode(developer)
    path = _sandbox_path(os.environ.get("PATH", ""), developer, xcode)
    # Under /private/tmp: a Unix socket's path has to fit in 104 bytes.
    with tempfile.TemporaryDirectory(prefix="cos-sandbox-", dir="/private/tmp") as tmp:
        tmp = os.path.realpath(tmp)
        readable = " ".join("(subpath %s)" % json.dumps(p) for p in SANDBOX_READ + xcode + (real, tmp))
        # stat() on the way down to its own folders, which may be inside the home folder.
        steps = {real, tmp}
        for p in (real, tmp):
            while p != os.path.dirname(p):
                p = os.path.dirname(p)
                steps.add(p)
        way_in = " ".join("(literal %s)" % json.dumps(p) for p in sorted(steps)) + \
            " (subpath %s) (subpath %s)" % (json.dumps(real), json.dumps(tmp))
        profile = SANDBOX_PROFILE % {"cwd": json.dumps(real), "tmp": json.dumps(tmp), "private": private,
                                     "readable": readable, "home": json.dumps(home_dir), "way_in": way_in}
        # CLAUDE_OS_DAEMON makes `cos` refuse every mutating command inside, too.
        env = {"PATH": path, "HOME": tmp, "TMPDIR": tmp + "/", "LANG": "en_US.UTF-8",
               "CLAUDE_OS_HOME": os.path.join(tmp, "state"), "COS_SANDBOXED": "1",
               "CLAUDE_OS_DAEMON": "sandboxed-tests"}
        argv = ["/usr/bin/env", "-i"] + ["%s=%s" % kv for kv in sorted(env.items())]
        return run(argv + ["sandbox-exec", "-p", profile] + list(cmd), timeout=timeout, cwd=real)


# Model text that could carry a command or a link to a notification: backticks, $, pipes, links.
LINK_OR_COMMAND = re.compile(r"[`$|]|://|www\.", re.I)


def plain_line(text, fallback, limit=180):
    """One line of model-written text, fit for a notification or the journal: compatibility
    forms folded (a fullwidth colon is a colon), no control, format or line-separator
    characters (bidi overrides, U+2028), and the fallback instead if it carries a command or a
    link."""
    import unicodedata
    out = []
    for c in unicodedata.normalize("NFKC", text or ""):
        cat = unicodedata.category(c)
        if cat in ("Zl", "Zp") or c in "\r\n\t":
            out.append(" ")
        elif not cat.startswith("C"):
            out.append(c)
    line = " ".join("".join(out).split())[:limit]
    return fallback if not line or LINK_OR_COMMAND.search(line) else line


def osascript(script, timeout=10):
    """Run AppleScript with a hard timeout.

    Apple Events block forever behind modal dialogs and permission prompts,
    so a timeout is mandatory, not optional.
    """
    return run(["osascript", "-e", script], timeout=timeout)


def which(name):
    return shutil.which(name)


def app_running(name, unknown=False):
    """Is the app running? -a: macOS pgrep skips its own ancestors unless asked, and the app's
    chat runs inside the app. pgrep says 0 (yes) or 1 (no); anything else (a timeout, an
    error) is `unknown`, which callers deciding something risky set to True."""
    rc = run(["pgrep", "-a", "-xq", name], timeout=3).rc
    return rc == 0 if rc in (0, 1) else unknown


def applescript_str(s):
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- formatting

def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def ago(ts):
    """'5m ago' style age for an epoch timestamp."""
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= size:
            return "%d%s ago" % (s // size, unit)
    return "just now"


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def emit(data, as_json, render):
    """Print data as JSON (for machines) or via render() (for humans)."""
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        render(data)


def die(msg, rc=1):
    print("cos: %s" % msg, file=sys.stderr)
    sys.exit(rc)


# ---------------------------------------------------------------- journal

def journal(kind, msg, **data):
    """Append one event to the private journal (monthly JSONL files)."""
    entry = {"ts": now_iso(), "kind": kind, "msg": msg}
    entry.update(data)
    path = home("journal", _dt.date.today().strftime("%Y-%m") + ".jsonl")
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    return entry


def new_here(days=7):
    """Is Ovenbird in its first week on this Mac? (The journal's first entry is that young, or
    there's no journal yet.) Today shows a welcome card then."""
    try:
        files = sorted(home("journal").glob("*.jsonl"))
        if not files:
            return True
        with open(files[0]) as f:
            first = _dt.datetime.fromisoformat(json.loads(f.readline())["ts"])
        return first > _dt.datetime.now().astimezone() - _dt.timedelta(days=days)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def read_journal(days=7, kind=None):
    cutoff = _dt.datetime.now().astimezone() - _dt.timedelta(days=days)
    entries = []
    jdir = home("journal")
    for path in sorted(jdir.glob("*.jsonl")):
        with open(path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if _dt.datetime.fromisoformat(e["ts"]) < cutoff:
                        continue
                except (ValueError, KeyError):
                    continue
                if kind and e.get("kind") != kind:
                    continue
                entries.append(e)
    return entries


# ---------------------------------------------------------------- json state

def same_folder(a, b):
    """The same folder, however its path is spelled (macOS keeps a path's case but ignores it)."""
    try:
        return os.path.samefile(str(a), str(b))
    except (OSError, ValueError, TypeError):  # missing, a NUL in the path, not a path
        return False


def same_repository(url, repository=None):
    """Is this git URL (https, ssh or scp-style) the repository, under its name now or a former
    one? Like the app's isRepository."""
    u = (url or "").strip().lower()
    for prefix in ("https://", "ssh://", "git@"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if "@" in u.split("/", 1)[0]:
        u = u.split("@", 1)[1]  # https://someone@github.com/…, ssh://git@github.com/…
    u = u.replace("github.com:", "github.com/", 1).rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    names = (repository,) if repository else (REPOSITORY,) + FORMER_REPOSITORIES
    return u in (n.lower() for n in names)


def install_record():
    """What Ovenbird.app's first launch wrote when it downloaded this checkout (decision 0015,
    step 5), or {}. A checkout someone cloned themselves has none."""
    d = load_json(home("state", "install.json"), {})
    root = d.get("root") if isinstance(d, dict) and d.get("kind") == "download" else None
    if not isinstance(root, str) or not same_folder(root, ROOT):
        return {}
    return d


def downloaded():
    """Is this a copy the app downloaded from the project's repository, which isn't the
    person's own? Its Claude doesn't push or merge, so it has no night shift. Someone who
    points it at their own fork gets it back."""
    if not install_record():
        return False
    origin = run(["git", "-C", str(ROOT), "config", "--get", "remote.origin.url"], timeout=5)
    return not origin.ok or same_repository(origin.out)  # an origin git can't read counts as ours


# A Claude started for someone other than the owner at their own terminal (the app's chat,
# `cos ask`) doesn't load .claude/settings.local.json: its allows are the owner's alone (0013).
SETTING_SOURCES = "user,project"


# What a permission rule looks like: a tool name, then maybe its pattern in brackets. Nothing
# else from that file reaches a command line, so no entry can become a flag (`--permission-mode…`).
RULE = re.compile(r"[A-Za-z][\w.-]*(\(.*\))?")


def local_denies():
    """The deny rules no choice of setting sources may drop, for --disallowedTools: whatever
    the person denied in .claude/settings.local.json, and a downloaded copy's DOWNLOAD_DENY,
    which lives in that same file (0015). That file can only take power away here."""
    d = load_json(ROOT / ".claude" / "settings.local.json", {})
    perms = d.get("permissions") if isinstance(d, dict) else None
    deny = perms.get("deny") if isinstance(perms, dict) else None
    rules = [r for r in deny if isinstance(r, str) and RULE.fullmatch(r)] if isinstance(deny, list) else []
    if downloaded():
        rules += [r for r in DOWNLOAD_DENY if r not in rules]
    return rules


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def cache_get(name, max_age=None):
    """Return cached payload, or None if missing/stale."""
    blob = load_json(home("cache", name + ".json"))
    if not blob:
        return None
    if max_age is not None and time.time() - blob.get("ts", 0) > max_age:
        return None
    return blob


def cache_put(name, payload, ts=None):
    blob = {"ts": time.time() if ts is None else ts, "data": payload}
    try:
        save_json(home("cache", name + ".json"), blob)
    except OSError:
        pass
    return blob


# ---------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "music": {"storefront": None, "apple_music": False},
    "weather": {},
    "notify": {"enabled": True, "native": False},
    "pulse": {"low_disk_gb": 20, "backup_check": True},
    # Only the human turns this on, in Ovenbird.app's own dialog: Keep Ovenbird up to date
    # (0011), which pins the origin it was turned on for.
    "autonomy": {"catch_up": False, "catch_up_url": None},
}


def config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = load_json(home("config.json"), {}) or {}
    for section, values in user.items():
        if isinstance(cfg.get(section), dict):
            # A section written as a plain value (a hand edit like `"autonomy": true`) is ignored
            # and keeps its defaults; `cos config` refuses to write one.
            if isinstance(values, dict):
                cfg[section].update(values)
        else:
            cfg[section] = values
    return cfg


def config_set(dotted, value):
    user = load_json(home("config.json"), {}) or {}
    node = user
    keys = dotted.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    save_json(home("config.json"), user)
    return user


# ---------------------------------------------------------------- network

REPOSITORY = "github.com/NobiResearch/Ovenbird"
# Its name until decision 0019. GitHub sends the old address on to the new one, and a copy
# cloned from it is still a copy of this repository. This is trusted forever, so never rename
# or delete the NobiResearch organization, and never create a repository named Claude-OS in
# it: either would let someone else serve code at the old address.
FORMER_REPOSITORIES = ("github.com/NobiResearch/Claude-OS",)
RELEASES = "https://%s/releases" % REPOSITORY
# What a downloaded copy's Claude may never do on its own (decision 0015): the repository isn't
# the person's. Ovenbird.app writes these into the copy's .claude/settings.local.json when it
# downloads it (FirstLaunch.localSettings, the same list), and system.keep_download_settings
# puts them back if they go missing.
DOWNLOAD_DENY = ["Bash(gh *)", "Bash(gh-axi *)", "Bash(git push*)", "Bash(git * push*)"]
# The oldest Ovenbird.app this code works with (decision 0015, step 6). Raise it when the
# server needs something only a newer app does, and a downloaded app that's older says so.
APP_NEEDED = "0.7.0"
USER_AGENT = "Ovenbird/0.1 (+https://%s)" % REPOSITORY
_SSL = None


def ssl_context():
    """python.org builds ship without CA certs; macOS keeps a bundle at /etc/ssl/cert.pem."""
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context()
        if os.path.exists("/etc/ssl/cert.pem"):
            try:
                _SSL.load_verify_locations("/etc/ssl/cert.pem")
            except (OSError, ssl.SSLError):
                pass
    return _SSL


def http_get(url, headers=None, timeout=12):
    """GET a URL; returns the open response (use as a context manager). Raises urllib errors."""
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def curl_get(url, timeout=12, max_bytes=5 * 1000 * 1000):
    """Fallback for sites that reject Python's TLS fingerprint. Returns bytes or None (also
    when the body would be larger than max_bytes: curl stops the transfer there)."""
    try:
        p = subprocess.run(["curl", "-sfL", "--max-time", str(timeout), "--max-filesize", str(max_bytes),
                            "-A", USER_AGENT, "--", url],
                           capture_output=True, timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


# ---------------------------------------------------------------- mac i/o

def app_url(path, **params):
    """A claude-os:// link for a command that changes something. It carries the per-launch
    nonce Ovenbird.app keeps in ~/.claude-os/state/app.nonce (0600), so a web page or any
    process that can't read that file can't make the app act."""
    import urllib.parse
    try:
        params["nonce"] = home("state", "app.nonce").read_text().strip()
    except OSError:
        params["nonce"] = ""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return "claude-os://%s?%s" % (path, query)


def app_record():
    """What Ovenbird.app wrote about itself when it last launched (state/app.json), or {}."""
    d = load_json(home("state", "app.json"), {})
    return d if isinstance(d, dict) else {}


def version_tuple(v):
    """'0.8.1' -> (0, 8, 1); None for anything else."""
    return tuple(int(p) for p in v.split(".")) if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+){0,3}", v) else None


def app_outdated():
    """A downloaded app older than this code needs: {"have", "needed"}, or None."""
    app = app_record()
    have, needed = version_tuple(app.get("version")), version_tuple(APP_NEEDED)
    if not app.get("release") or not have or have >= needed:
        return None
    return {"have": app["version"], "needed": APP_NEEDED}


def _is_claude_os_app(path):
    """An Ovenbird.app bundle: our executable, and it handles claude-os:// links."""
    import plistlib
    try:
        with open(os.path.join(str(path), "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except Exception:
        return False
    schemes = [s for t in info.get("CFBundleURLTypes") or [] if isinstance(t, dict)
               for s in t.get("CFBundleURLSchemes") or []]
    return str(path).endswith(".app") and info.get("CFBundleExecutable") == "ClaudeOS" and "claude-os" in schemes


def app_bundle():
    """The Ovenbird.app to talk to: the one that last launched (a downloaded release or a
    developer's own build, wherever it is), if it's still there and is Ovenbird; else
    ~/Applications/Ovenbird.app; else None (decision 0015)."""
    for path in (app_record().get("bundle"), os.path.expanduser("~/Applications/Ovenbird.app")):
        if path and _is_claude_os_app(path):
            return str(path)
    return None


def open_app_url(url, timeout=15):
    """Hand a claude-os:// link to the Ovenbird.app in use (`open -a`), never to whichever copy
    macOS prefers: with two copies on a Mac, each would get the other's links (0015)."""
    bundle = app_bundle()
    return run(["open", "-g"] + (["-a", bundle] if bundle else []) + [url], timeout=timeout)


def ensure_app(wait=12):
    """Start Ovenbird.app (in the background) if it isn't running; True once it's up."""
    if app_running("ClaudeOS"):
        return True
    bundle = app_bundle()
    if not bundle:
        return False
    nonce = home("state", "app.nonce")
    before = nonce.stat().st_mtime if nonce.exists() else 0
    run(["open", "-g", bundle], timeout=15)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        if app_running("ClaudeOS") and nonce.exists() and nonce.stat().st_mtime > before:
            return True
    return app_running("ClaudeOS")


def native_notifications():
    """Ovenbird.app posts them itself when the human turned it on, macOS allowed it and the
    app is running. Background jobs never launch the app just to show a notification."""
    if not config()["notify"].get("native"):
        return False
    status = load_json(home("state", "notify.json"), {}) or {}
    return status.get("authorization") == "authorized" and app_running("ClaudeOS")


# The only places a click on a notification may open. The app checks the same list and maps
# each to a fixed view; a click only ever navigates.
NOTIFY_TARGETS = ("board", "brief", "evolve", "daemons")


def notify(message, title="Ovenbird", subtitle=None, sound=None, target=None):
    if not config()["notify"]["enabled"]:
        return Result(0, "", "notifications disabled")
    if native_notifications():
        # The text goes in a private file; the link that wakes the app carries nothing.
        import uuid
        note = {"title": title, "subtitle": subtitle or "", "body": message, "sound": bool(sound)}
        if target in NOTIFY_TARGETS:
            note["target"] = target
        path = home("state", "outbox", "%d-%s.json" % (int(time.time() * 1000), uuid.uuid4().hex[:8]))
        try:
            with open(path, "w") as f:
                json.dump(note, f, ensure_ascii=False)
            os.chmod(str(path), 0o600)
            r = open_app_url("claude-os://notify/flush", timeout=10)
            if r.ok:
                return r
            os.unlink(str(path))  # couldn't wake the app: say it the old way instead
        except OSError:
            pass  # fall through to AppleScript
    script = "display notification %s with title %s" % (
        applescript_str(message), applescript_str(title))
    if subtitle:
        script += " subtitle " + applescript_str(subtitle)
    if sound:
        script += " sound name " + applescript_str(sound)
    return osascript(script, timeout=5)


def locale_country():
    """Two-letter storefront from the system locale, e.g. en_GB -> gb."""
    r = run(["defaults", "read", "NSGlobalDomain", "AppleLocale"], timeout=3)
    loc = r.out if r.ok else ""
    if "_" in loc:
        return loc.split("_")[1][:2].lower()
    return "us"
