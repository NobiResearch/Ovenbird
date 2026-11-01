"""cos update — what's out of date on this Mac, and upgrading it when asked."""

import datetime as dt
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import core

BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"}


def parse_softwareupdate(text):
    """Parse `softwareupdate -l` into [{label, title, version, size, restart, recommended}]."""
    items, current = [], None
    for line in text.splitlines():
        m = re.match(r"\s*\*\s+Label:\s+(.+)", line)
        if m:
            current = {"label": m.group(1).strip()}
            items.append(current)
            continue
        if current is not None and "Title:" in line:
            fields = dict((k.strip().lower(), v.strip()) for k, v in
                          (kv.split(":", 1) for kv in line.strip().rstrip(",").split(", ") if ":" in kv))
            current["title"] = fields.get("title")
            current["version"] = fields.get("version")
            current["size"] = fields.get("size")
            current["recommended"] = fields.get("recommended", "").upper() == "YES"
            current["restart"] = "restart" in fields.get("action", "").lower()
    return items


def macos_name(it):
    """"macOS 27.2", not "macOS 27.2 27.2": the title usually has the version in it already."""
    title, version = it.get("title") or it.get("label") or "", it.get("version") or ""
    return title if version in title else ("%s %s" % (title, version)).strip()


def check_macos():
    r = core.run(["softwareupdate", "-l"], timeout=120)
    text = r.out + "\n" + r.err
    return {"items": parse_softwareupdate(text), "error": None if ("Finding" in text or r.ok) else text[:200]}


def parse_brew_outdated(text):
    data = json.loads(text or "{}")
    items = []
    for f in data.get("formulae", []):
        items.append({"name": f["name"], "kind": "formula", "installed": (f.get("installed_versions") or ["?"])[-1],
                      "latest": f.get("current_version"), "pinned": f.get("pinned", False)})
    for c in data.get("casks", []):
        items.append({"name": c["name"], "kind": "cask", "installed": (c.get("installed_versions") or ["?"])[-1],
                      "latest": c.get("current_version"), "pinned": False})
    for it in items:
        _classify(it)
    return items


def _major(v):
    m = re.match(r"(\d+)", str(v or ""))
    return m.group(1) if m else v


def _version(v):
    """'3.6.5_1' -> ((3, 6, 5), 1): leading numbers (trailing zeros dropped, so 1.0 == 1.0.0)
    plus brew's revision; a cask's ',build' is dropped."""
    m = re.match(r"(\d+(?:\.\d+)*)(?:_(\d+))?", str(v or "").split(",")[0])
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts), int(m.group(2) or 0)


def _look(installed, latest):
    """Why an upgrade deserves a look first: 'scheme', 'downgrade', 'major', or None."""
    a, b = _version(installed), _version(latest)
    if a and b:
        if (a[0][0] >= 1000) != (b[0][0] >= 1000):
            return "scheme"  # a build number or year became a version (or back): not comparable
        if b < a:
            return "downgrade"
    return "major" if _major(installed) != _major(latest) else None


def _classify(it):
    it["look"] = _look(it["installed"], it["latest"])
    it["major"] = it["look"] == "major"
    return it


def check_brew(fetch=False):
    if not core.which("brew"):
        return None
    if fetch:
        core.run(["brew", "update", "--quiet"], timeout=300, env=BREW_ENV)
    r = core.run(["brew", "outdated", "--json=v2"], timeout=120, env=BREW_ENV)
    try:
        return {"items": parse_brew_outdated(r.out), "error": None}
    except ValueError:
        return {"items": [], "error": (r.err or r.out)[:200]}


# A fixed phrase, never the tool's own words: npm's errors name log files under the home folder.
UNCHECKED = "couldn't check"


# npm's http log (stderr): "npm http fetch GET 403 <url> 12ms", or "npm http cache <url> …"
NPM_HTTP = re.compile(r"\bhttp (?:fetch [A-Z]+ (\d{3}) |cache )")


def npm_refused(err):
    """Did the registry refuse everything npm asked it (403s, and no answer)? `npm outdated`
    skips a package whose lookup comes back 403 without a word, taking it for a private one,
    so behind a proxy or firewall that refuses the registry it prints {} and exits 0."""
    answers = NPM_HTTP.findall(err or "")
    return "403" in answers and not any(a == "" or int(a) < 400 for a in answers)


def parse_npm_outdated(rc, out, err=""):
    """`npm outdated -g --json --loglevel=http` -> items, or None if npm couldn't check. Exit 1
    with packages is normal (something is outdated); an "error" object, a failure with nothing
    listed, or a registry that refused every lookup is npm not reaching its registry (offline,
    sandboxed, behind a proxy), which isn't "up to date"."""
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict) or "error" in data or (rc != 0 and not data) or npm_refused(err):
        return None
    return [_classify({"name": name, "kind": "npm", "installed": v.get("current"), "latest": v.get("latest")})
            for name, v in data.items() if isinstance(v, dict)]


def check_npm():
    if not core.which("npm"):
        return None
    r = core.run(["npm", "outdated", "-g", "--json", "--loglevel=http", "--color=false"], timeout=90)
    items = parse_npm_outdated(r.rc, r.out, r.err)
    return {"items": items or [], "error": None if items is not None else UNCHECKED}


def check_mas():
    if not core.which("mas"):
        return None
    r = core.run(["mas", "outdated"], timeout=60)
    items = []
    for line in r.out.splitlines():
        m = re.match(r"(\d+)\s+(.+?)\s+\((.+?)\s*->\s*(.+?)\)", line)
        if m:
            items.append({"name": m.group(2), "id": m.group(1), "kind": "app-store",
                          "installed": m.group(3), "latest": m.group(4)})
    return {"items": items, "error": UNCHECKED if not r.ok and not items else None}


def check_rustup():
    if not core.which("rustup"):
        return None
    r = core.run(["rustup", "check"], timeout=60)
    items = []
    for line in r.out.splitlines():
        if "Update available" in line:
            name = line.split(" - ")[0].strip()
            vers = re.findall(r"(\d+\.\d+\.\d+)", line)
            items.append({"name": name, "kind": "rustup", "installed": vers[0] if vers else "?",
                          "latest": vers[-1] if vers else "?"})
    # rustup check exits 100 when updates are available; only a failure with nothing parsed is unchecked
    return {"items": items, "error": UNCHECKED if r.rc not in (0, 100) and not items else None}


def check_claude_os():
    """Ovenbird itself: what's merged on GitHub and not on this Mac yet (decision 0011). A
    downloaded copy follows its app instead (0015), so main is none of its business."""
    from . import evolve
    if core.downloaded():
        return None
    c = evolve.catch_up(fetch=True, source="update")
    items = [{"name": "Ovenbird", "installed": c["head"], "latest": c["latest"], "changes": c["subjects"],
              "protected": c["protected"]}] if c["behind"] else []
    return {"items": items, "error": c["why"] if items else None}


LATEST_RELEASE = "https://api.github.com/repos/%s/releases/latest" % core.REPOSITORY.split("/", 1)[1]
RELEASE_PAGE = re.compile(r"https://github\.com/%s/releases/tag/[\w.-]+" % re.escape(core.REPOSITORY.split("/", 1)[1]), re.I)
LOOK_EVERY = 86400
LOOKING = threading.Lock()  # the desktop's status poll and the Updates window can arrive together
unrecorded = {}  # a look the cache couldn't keep (a full disk): the polls mustn't ask again each minute


def usable(latest):
    """Is this a release we'd link to?"""
    return (isinstance(latest, dict) and core.version_tuple(latest.get("version"))
            and isinstance(latest.get("url"), str) and RELEASE_PAGE.fullmatch(latest["url"]))


def last_look():
    """The last look, {"ts", "latest"}, or None. A damaged file counts as no look, and so does
    a date to come (a clock that was wrong), so the next one writes it again."""
    kept = dict(unrecorded)  # a copy: signals read this without LOOKING, and a look can clear it
    looks = [kept] if kept else []
    blob = core.cache_get("app-release")
    if blob and blob["ts"] <= time.time() and isinstance(blob["data"], dict) and "latest" in blob["data"]:
        latest = blob["data"]["latest"]
        if latest is None or usable(latest):
            looks.append({"ts": blob["ts"], "latest": latest})
    return max(looks, key=lambda look: look["ts"], default=None)


def latest_release():
    """The newest Ovenbird.app on GitHub, {"version", "url"}, or None. One look a day at
    most, whatever the answer (decision 0015, step 6); one that finds nothing keeps what the
    last one found, so a day offline doesn't take the line away."""
    with LOOKING:
        look = last_look()
        if look and time.time() - look["ts"] <= LOOK_EVERY:
            return look["latest"]
        latest = look["latest"] if look else None
        try:
            with core.http_get(LATEST_RELEASE, headers={"Accept": "application/vnd.github+json"}, timeout=8) as r:
                d = json.loads(r.read(500000))
            tag = d.get("tag_name")
            found = {"version": tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag, "url": d.get("html_url")}
            if usable(found) and not d.get("draft"):
                latest = found
        except Exception:  # offline, rate-limited, no release yet (404): ask again tomorrow
            pass
        unrecorded.clear()
        blob = core.cache_put("app-release", {"latest": latest})
        if (core.cache_get("app-release") or {}).get("ts") != blob["ts"]:
            unrecorded.update(ts=blob["ts"], latest=latest)
        return latest


def fold_app():
    """Put today's look into the cached results, which can be any age: without pulse nothing
    else refreshes them. Their ts stays, since "checked" is about everything else on the Mac
    and pulse goes by it. With no results yet there's nothing to add to: the signal says so
    (known_newer_app), and Updates checks all of it when it opens. It runs on every status
    poll, so it only writes when the line changes."""
    try:
        if LOOKING.locked():  # nothing waits for a look under way: the next poll brings it in
            return
        fresh = check_app()
        cached = core.cache_get("updates")
        if fresh is None or not cached:
            return
        sources = cached["data"]["sources"]
        if sources.get("app") != fresh:
            sources["app"] = fresh
            cached["data"]["total"] = sum(len(v["items"]) for v in sources.values())
            core.cache_put("updates", cached["data"], ts=cached["ts"])
    except Exception:  # damaged results are served as they were
        pass


def newer_app(app, latest):
    """Updates' line for `latest`, if it's newer than this downloaded app, or None. Updates
    and the signal both ask here, so the rule can't drift between them."""
    have = core.version_tuple(app.get("version"))
    if not app.get("release") or not latest or not have or core.version_tuple(latest["version"]) <= have:
        return None
    return {"name": "Ovenbird.app", "installed": app["version"], "latest": latest["version"], "url": latest["url"]}


def known_newer_app():
    """What the last look found, for status.signals (0026). They run on every poll, in the
    boot banner and in pulse, unguarded: so no network, no LOOKING, nothing written, and None
    for anything unreadable. With no release app on record, it reads no look at all."""
    try:
        app = core.app_record()
        return newer_app(app, (last_look() or {}).get("latest")) if app.get("release") else None
    except Exception:
        return None


def check_app():
    """A newer Ovenbird.app, for a downloaded one: it never replaces itself (0015), so it
    says so here, quietly. None for an app someone built themselves."""
    app = core.app_record()
    if not app.get("release"):
        return None
    item = newer_app(app, latest_release())
    return {"items": [item] if item else [], "error": None}


def check(fetch=False):
    jobs = {"macos": check_macos, "brew": lambda: check_brew(fetch), "npm": check_npm,
            "mas": check_mas, "rustup": check_rustup, "claude-os": check_claude_os, "app": check_app}
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        sources = {k: f.result() for k, f in futures.items()}
    sources = {k: v for k, v in sources.items() if v is not None}
    total = sum(len(v["items"]) for v in sources.values())
    data = {"sources": sources, "total": total, "fetched": fetch,
            "missing_tools": [t for t in ("mas",) if not core.which(t)]}
    core.cache_put("updates", data)
    return data


MARKS = {"major": "▲", "downgrade": "▼", "scheme": "≠"}
NOTES = {"downgrade": " (downgrade)", "scheme": " (new version scheme)"}


def _look_of(it):
    return it.get("look") or ("major" if it.get("major") else None)  # caches from before `look` existed


def render(data):
    src = data["sources"]
    mac = src.get("macos", {}).get("items", [])
    if mac:
        print("macOS")
        for it in mac:
            print("  ⚠ %s%s" % (macos_name(it), " (restart)" if it.get("restart") else ""))
    else:
        print("macOS: up to date")
    for name, label in (("brew", "Homebrew"), ("npm", "npm (global)"), ("mas", "App Store"), ("rustup", "Rust")):
        s = src.get(name)
        if not s:
            continue
        items = s["items"]
        if not items:
            print("%s: %s" % (label, UNCHECKED if s.get("error") == UNCHECKED else "up to date"))
            continue
        looks = [i for i in items if _look_of(i)]
        print("%s: %d outdated%s" % (label, len(items), (", %d to look at first" % len(looks)) if looks else ""))
        for it in sorted(items, key=lambda i: (not _look_of(i), i["name"]))[:12]:
            look = _look_of(it)
            print("  %s %-24s %s → %s%s%s" % (MARKS.get(look, "·"), it["name"], it["installed"], it["latest"],
                                             NOTES.get(look, ""), " (pinned)" if it.get("pinned") else ""))
        if len(items) > 12:
            print("  … and %d more" % (len(items) - 12))
    render_claude_os(src.get("claude-os"))
    for it in (src.get("app") or {}).get("items", []):
        print("Ovenbird.app %s is out (you have %s): %s" % (it["latest"], it["installed"], it["url"]))
    if not data["fetched"] and src.get("brew"):
        print("\n(brew metadata not refreshed; `cos update --fetch` asks Homebrew for the latest)")
    if data["missing_tools"]:
        print("tip: `brew install mas` lets Ovenbird see App Store updates too")
    print("\nupgrade with: cos update apply brew|npm|mas|rustup [names...] --yes · macOS: cos update apply macos"
          + (" · Ovenbird: cos update apply claude-os --yes" if (src.get("claude-os") or {}).get("items") else ""))


def render_claude_os(s):
    if not s or not s["items"]:
        return
    it = s["items"][0]
    n = len(it["changes"])
    print("Ovenbird: %d change%s merged on GitHub, not on this Mac (%s → %s)"
          % (n, "" if n == 1 else "s", it["installed"], it["latest"]))
    for subject in it["changes"][:8]:
        print("  · %s" % subject)
    if n > 8:
        print("  … and %d more" % (n - 8))
    if it["protected"]:
        print("  ! the gate changes too: %s" % ", ".join(it["protected"][:6]))
    if s.get("error"):
        print("  not updated: %s" % s["error"])


PROGRESS = re.compile(r"^(==> (Upgrading|Installing|Pouring|Summary)\b|added \d+|changed \d+|updated \d+|info: )")
NOTICE = re.compile(r"^(Warning|Error|npm (warn|error|ERR!)|error(\[|:))", re.I)
BLOCK_END = re.compile(r"^(==> |🍺|added \d+|changed \d+|\S+: )")


def notices(lines, cap=40):
    """Every Warning:/Error: block with the lines that belong to it (brew lists the files a
    `brew link` overwrote under its warning), so the details aren't cut off by a tail."""
    blocks, block = [], None
    for line in lines:
        text = line.rstrip()
        if NOTICE.match(text):
            block = [text]
            blocks.append(block)
        elif block is not None and text and not BLOCK_END.match(text) and len(block) < cap:
            block.append(text)
        else:
            block = None
    return blocks


def still_outdated(names):
    """After `brew upgrade <names>`: the ones that are still outdated, so a package brew
    fetched but didn't install doesn't go unreported."""
    r = core.run(["brew", "outdated", "--json=v2"] + names, timeout=120, env=BREW_ENV)
    try:
        return [i["name"] for i in parse_brew_outdated(r.out)]
    except (ValueError, KeyError, TypeError):
        return []


def apply_claude_os(yes):
    """Fast-forward the live checkout to main on GitHub, through evolve.catch_up's guards."""
    from . import evolve
    if core.downloaded():
        core.die("a downloaded Ovenbird follows its app: download the newest Ovenbird.app from %s" % core.RELEASES)
    c = evolve.catch_up(fetch="force", source="update", retry=True)
    if not c["behind"]:
        print("Ovenbird is up to date with GitHub (%s)." % c["head"])
        return 0
    if c["why"]:
        core.die("Ovenbird stays at %s: %s." % (c["head"], c["why"]))
    if not yes:
        render_claude_os(check_claude_os())
        print("\nWould fast-forward this checkout to %s. (add --yes to do it)" % c["latest"])
        return 2
    c = evolve.catch_up(apply=True, fetch=False, source="update", retry=True)
    if not c["updated"]:
        core.die("Ovenbird stays at %s: %s." % (c["head"], c["why"]))
    print("✓ Ovenbird is at %s now:" % c["head"])
    for subject in c["subjects"][:12]:
        print("  · %s" % subject)
    if c["protected"]:
        print("  ! the gate changed: %s" % ", ".join(c["protected"][:6]))
    print("\nNew sessions use it. This one read the old CLAUDE.md and skills: read CLAUDE.md again for new commands.")
    if any(p.startswith("etc/daemons.json") for p in c["paths"]):
        print("The daemons changed: `cos daemon install --all` reloads them.")
    if core.app_running("ClaudeOS") and any(p.startswith(("lib/", "ui/", "app/", "bin/")) for p in c["paths"]):
        rebuild = any(p.startswith("app/") for p in c["paths"])
        print("Ovenbird.app still runs the old code: %squit and reopen it." % ("rebuild it (`cos app install`), then " if rebuild else ""))
    core.run(["rm", "-f", str(core.home("cache", "updates.json"))])
    return 0


def apply(source, names, yes=False):
    if source == "claude-os":
        return apply_claude_os(yes)
    plans = {
        "brew": ["brew", "upgrade"] + names,
        "npm": (["npm", "install", "-g"] + ["%s@latest" % n for n in names]) if names else ["npm", "update", "-g"],
        "mas": ["mas", "upgrade"] + names,
        "rustup": ["rustup", "update"],
        "macos": ["open", "x-apple.systempreferences:com.apple.Software-Update-Settings.extension"],
    }
    if source not in plans:
        core.die("unknown source %r (choose: %s)" % (source, ", ".join(plans)))
    cmd = plans[source]
    if source == "macos":
        core.run(cmd)
        print("Opened Software Update in System Settings. macOS installs need your password and a restart,"
              " so the human presses the button.")
        return 0
    if not yes:
        print("Would run: %s\n(add --yes to do it)" % " ".join(cmd))
        return 2
    print("$ %s" % " ".join(cmd), flush=True)
    lines = []

    def on_line(line):
        lines.append(line)
        if PROGRESS.match(line):
            print("  " + line.rstrip(), flush=True)

    # stderr goes into stdout ("$0" "$@" keeps every argument as it is), so a warning arrives
    # next to what it's about instead of both streams' tails glued together
    r = core.stream(["/bin/sh", "-c", 'exec "$0" "$@" 2>&1'] + cmd, on_line, timeout=3600,
                    env=BREW_ENV if source == "brew" else None)
    log = core.home("runs", "updates", dt.datetime.now().strftime("%Y-%m-%d-%H%M-") + source + ".log")
    try:
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(lines) + (("\n" + r.err) if r.err else ""))
    except OSError:
        log = None
    for block in notices(lines):
        print("\n" + "\n".join("  " + l for l in block))
    left = still_outdated(names) if source == "brew" and names and r.rc != 124 else []
    if left:
        print("\nStill outdated after the upgrade: %s" % ", ".join(left))
    if r.rc == 124:
        print("\nStopped after an hour; it may have finished part of the list.")
    elif not r.ok:
        print("\nFinished with errors (exit %d)." % r.rc)
    if log:
        print("\nFull output: %s" % log)
    core.journal("action", "update %s %s (rc=%d)%s" % (source, " ".join(names) or "all", r.rc,
                                                       "; still outdated: " + ", ".join(left) if left else ""),
                 source=source, names=names, rc=r.rc, still_outdated=left)
    core.run(["rm", "-f", str(core.home("cache", "updates.json"))])
    return 0 if r.ok else 1


def main(args):
    if args.update_cmd == "apply":
        return apply(args.source, args.names, yes=args.yes)
    data = check(fetch=args.fetch)
    core.emit(data, args.json, render)
    return 0
