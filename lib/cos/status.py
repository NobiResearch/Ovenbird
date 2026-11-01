"""cos status — vitals of the Mac, plus signals worth acting on."""

import os
import re
import time

from . import core

DATA_VOLUME = "/System/Volumes/Data"


# ps and sysctl write decimals the locale's way ("12,5" in Norwegian, say), and Ovenbird
# parses them, so they always run in the C locale.
C_LOCALE = {"LC_ALL": "C"}


def _sysctl(name):
    r = core.run(["sysctl", "-n", name], timeout=3, env=C_LOCALE)
    return r.out if r.ok else ""


def known(*parts, sep=" · "):
    """The parts that could be read, joined. A sandbox (Claude Code's, say) refuses sysctl and
    ps, which answer the model, chip, cores, memory size, swap, uptime, load and top processes:
    a shorter line beats "0 cores" or "used of 0 B"."""
    return sep.join(p for p in parts if p)


def disk():
    path = DATA_VOLUME if os.path.exists(DATA_VOLUME) else "/"
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "free": free, "free_pct": round(100.0 * free / total, 1) if total else 0}


def memory():
    total = int(_sysctl("hw.memsize") or 0)
    r = core.run(["vm_stat"], timeout=3)
    page = 16384
    m = re.search(r"page size of (\d+)", r.out)
    if m:
        page = int(m.group(1))
    pages = {}
    for line in r.out.splitlines()[1:]:
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            pages[k.strip()] = int(v)
    used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)) * page
    sm = re.search(r"used = ([\d.]+)M", _sysctl("vm.swapusage"))
    swap = int(float(sm.group(1)) * 1024 * 1024) if sm else None  # None: couldn't tell
    return {"total": total, "used": used, "swap_used": swap}


def battery():
    r = core.run(["pmset", "-g", "batt"], timeout=3)
    m = re.search(r"(\d+)%;\s*([^;]+);", r.out)
    if not m:
        return None
    info = {"percent": int(m.group(1)), "state": m.group(2).strip(),
            "source": "AC" if "AC Power" in r.out else "battery"}
    io = core.run(["ioreg", "-rn", "AppleSmartBattery"], timeout=3).out
    cyc = re.search(r'"CycleCount" = (\d+)', io)
    raw = re.search(r'"AppleRawMaxCapacity" = (\d+)', io)
    design = re.search(r'"DesignCapacity" = (\d+)', io)
    pct = re.search(r'"MaxCapacity" = (\d+)', io)
    if cyc:
        info["cycles"] = int(cyc.group(1))
    if raw and design and int(design.group(1)):
        info["health_pct"] = min(100, round(100.0 * int(raw.group(1)) / int(design.group(1))))
    elif pct and int(pct.group(1)) <= 100:
        info["health_pct"] = int(pct.group(1))  # Apple Silicon reports health directly
    return info


def uptime_seconds():
    m = re.search(r"sec = (\d+)", _sysctl("kern.boottime"))
    return int(time.time()) - int(m.group(1)) if m else None


def load():
    nums = re.findall(r"[\d.]+", _sysctl("vm.loadavg"))
    return [float(x) for x in nums[:3]]


def app_name(comm):
    """Who a process belongs to: the outermost .app in its path when the process is that
    app's own (under Contents/MacOS or Contents/Frameworks), so Firefox's plugin-containers
    and Chrome's helpers count as Firefox and Google Chrome. Tools that merely live in a
    bundle (git inside Xcode.app, run through xcrun) keep their own name."""
    m = re.search(r"/([^/]+)\.app/(Contents/(MacOS|Frameworks)/)", comm)
    return m.group(1) if m else os.path.basename(comm)


def parse_ps(text):
    """`ps -Ao pid=,pcpu=,rss=,comm=` -> one row per app: summed CPU and memory, how many
    processes, and the pid of the biggest one."""
    groups = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) != 4:
            continue
        pid, cpu, rss, comm = parts
        try:
            cpu, rss = float(cpu), int(rss) * 1024
        except ValueError:
            continue
        name = app_name(comm)
        g = groups.setdefault(name, {"name": name, "cpu": 0.0, "rss": 0, "count": 0, "pid": int(pid), "_big": -1})
        g["cpu"] += cpu
        g["rss"] += rss
        g["count"] += 1
        if rss > g["_big"]:
            g["pid"], g["_big"] = int(pid), rss
    rows = list(groups.values())
    for g in rows:
        del g["_big"]
        g["cpu"] = round(g["cpu"], 1)
    return rows


def top_processes(n=5):
    rows = parse_ps(core.run(["ps", "-Ao", "pid=,pcpu=,rss=,comm="], timeout=5, env=C_LOCALE).out)
    return {"cpu": sorted(rows, key=lambda p: -p["cpu"])[:n], "memory": sorted(rows, key=lambda p: -p["rss"])[:n]}


def _proc(p, value):
    return "%s%s %s" % (p["name"], " ×%d" % p["count"] if p.get("count", 1) > 1 else "", value)


def security():
    from . import security as sec  # the reader lives with the one switch that acts on it
    return sec.posture()


def parse_backup(text):
    from . import backup as bk  # the reader lives with `cos backup` (decision 0012)
    return bk.parse_backup(text)


def backup():
    from . import backup as bk
    return bk.state()


def refresh_backup(max_age=24 * 3600):
    """Keep the `backup` cache that signals() reads; off with `cos config pulse.backup_check false`."""
    if core.config()["pulse"].get("backup_check", True) and not core.cache_get("backup", max_age=max_age):
        core.cache_put("backup", backup())


def system():
    v = core.run(["sw_vers"], timeout=3).out
    fields = dict(re.findall(r"(\w+):\s+(.+)", v))
    return {
        "macos": fields.get("ProductVersion"),
        "build": fields.get("BuildVersion"),
        "model": _sysctl("hw.model"),
        "chip": _sysctl("machdep.cpu.brand_string"),
        "cores": int(_sysctl("hw.ncpu") or 0),
    }


def signals(s):
    """Turn raw vitals into a short list of things worth a human's attention."""
    out = []
    d = s["disk"]
    pulse = core.config()["pulse"]
    low_gb = pulse["low_disk_gb"]
    if d["free"] < low_gb * 1024 ** 3 or d["free_pct"] < 10:
        out.append(("warn", "disk low: %s free" % core.human_bytes(d["free"])))
    elif d["free_pct"] < 20:
        out.append(("note", "disk getting full: %s%% free" % d["free_pct"]))
    m = s["memory"]
    if (m["swap_used"] or 0) > 4 * 1024 ** 3:
        out.append(("note", "heavy swap: %s" % core.human_bytes(m["swap_used"])))
    b = s.get("battery")
    if b and b.get("health_pct") and b["health_pct"] < 80:
        out.append(("note", "battery health %d%%" % b["health_pct"]))
    sec = s["security"]
    if sec.get("filevault") is False:
        out.append(("warn", "FileVault is off"))
    if sec.get("firewall") is False:
        out.append(("note", "firewall is off"))
    up = s.get("uptime") or 0
    if up > 14 * 86400:
        out.append(("note", "up %d days; a restart helps updates land" % (up // 86400)))
    wx = core.cache_get("weather", max_age=3 * 3600)
    if wx:
        from . import weather
        a = weather.as_of(wx["data"] or {}).get("alert") or {}
        if a.get("msg"):
            out.append((a["level"], a["msg"][0].upper() + a["msg"][1:]))
    from . import evolve, updates
    stuck = evolve.follow_problem()
    if stuck:
        out.append(("warn", "Ovenbird's code couldn't follow its app: %s" % stuck))
    older = core.app_outdated()
    if older:
        out.append(("warn", "this Ovenbird needs Ovenbird.app %s or newer (this one is %s): download it from %s"
                    % (older["needed"], older["have"], core.RELEASES)))
    newer = None if older else updates.known_newer_app()
    if newer:  # always a note: pulse turns a warn into a notification, and this never is one (0026)
        out.append(("note", "Ovenbird.app %s is out: see Updates" % newer["latest"]))
    upd = core.cache_get("updates")
    if upd:
        n = upd["data"].get("total", 0)
        if n:
            out.append(("note", "%d updates available (checked %s)" % (n, core.ago(upd["ts"]))))
    scan = core.cache_get("clean")
    if scan:
        safe = scan["data"].get("safe_bytes", 0)
        if safe > 5 * 1024 ** 3:
            out.append(("note", "%s safely reclaimable (scanned %s)" % (
                core.human_bytes(safe), core.ago(scan["ts"]))))
    tm = core.cache_get("backup")
    if tm and pulse.get("backup_check", True) and (tm["data"] or {}).get("configured") is False:
        out.append(("note", "Time Machine isn't set up: cos backup"))
    return out


def collect(include_slow=True):
    s = {
        "system": system(),
        "disk": disk(),
        "memory": memory(),
        "battery": battery(),
        "uptime": uptime_seconds(),
        "load": load(),
    }
    if include_slow:
        s["security"] = security()
        s["top"] = top_processes()
    else:
        s["security"] = {}
    s["signals"] = [{"level": lvl, "msg": msg} for lvl, msg in signals(s)]
    return s


TRENDS_FILE = "trends.jsonl"
TRENDS_KEEP = 730  # two years of daily lines


def record_trend(s, today=None):
    """Append one snapshot per day (idempotent), for trend lines once enough days exist.
    False when there's nothing to add, or when the file couldn't be read or written: then it's
    left as it was, and pulse goes on to its disk warning (a full disk is when writes fail)."""
    import datetime as dt
    import json
    today = today or dt.date.today().isoformat()
    path = core.home(TRENDS_FILE)
    try:
        # surrogateescape: bytes that aren't UTF-8 go back into the file exactly as they were.
        lines = _trend_lines(_read_trends(path, "surrogateescape"))
    except FileNotFoundError:
        lines = []
    except OSError as e:  # unreadable isn't empty: writing now would replace the whole history
        core.journal("daemon", "couldn't read %s, so today's trend wasn't recorded: %s" % (TRENDS_FILE, e), ok=False)
        return False
    last = next((row for row in map(_trend_row, reversed(lines)) if row), None)
    if last and last["date"] == today:
        return False
    b = s.get("battery") or {}
    upd = core.cache_get("updates")
    scan = core.cache_get("clean")
    row = {"date": today, "disk_free": s["disk"]["free"], "battery_health": b.get("health_pct"),
           "battery_cycles": b.get("cycles"), "updates": upd["data"].get("total") if upd else None,
           "reclaimable": scan["data"].get("safe_bytes") if scan else None}
    lines = (lines + [json.dumps(row)])[-TRENDS_KEEP:]
    # Into a file beside it, then renamed over it: a crash or a full disk mid-write can't cut the
    # history short, and fsync makes a power cut far less likely to (macOS's fsync stops short of
    # the drive's own cache). The temp file is private and never a link followed.
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        core.journal("daemon", "couldn't write %s, so today's trend wasn't recorded: %s" % (TRENDS_FILE, e), ok=False)
        return False
    return True


def _read_trends(path, errors):
    """trends.jsonl as text, with newline="": text mode would turn \\r\\n and \\r into \\n."""
    with open(path, encoding="utf-8", errors=errors, newline="") as f:
        return f.read()


def _trend_lines(text):
    """trends.jsonl's lines, split on "\\n" only: splitlines() would also split on \\r, \\f, U+2028
    and more, and the next record would write those back as "\\n"."""
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _trend_row(line):
    """One line of trends.jsonl as a row, or None if it's damaged: not a JSON object, or without
    the YYYY-MM-DD date and the finite numbers (or nulls) that disk_trend() and Vitals' Trends
    card read. (The browser can't parse Infinity or NaN.)"""
    import datetime as dt
    import json
    import math
    try:
        row = json.loads(line)
        if dt.date.fromisoformat(row["date"]).isoformat() != row["date"]:
            return None  # Python 3.11+ also accepts "20261029"
    except (ValueError, TypeError, KeyError):
        return None
    for key in ("disk_free", "battery_health"):
        v = row.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                              or (isinstance(v, float) and not math.isfinite(v))):
            return None
    return row


def trends():
    """The daily rows, oldest first. A damaged line is skipped rather than costing the rest,
    so it can't break Vitals' Trends card, disk_trend() or the next day's record."""
    try:
        lines = _trend_lines(_read_trends(core.home(TRENDS_FILE), "replace"))
    except OSError:
        return []
    return [row for row in map(_trend_row, lines) if row is not None]


def disk_trend(rows, min_days=7):
    """Average change in free disk per week, once there's at least a week of data."""
    rows = [r for r in rows if r.get("disk_free") is not None]
    if len(rows) < min_days:
        return None
    import datetime as dt
    first, last = rows[0], rows[-1]
    days = (dt.date.fromisoformat(last["date"]) - dt.date.fromisoformat(first["date"])).days or 1
    return (last["disk_free"] - first["disk_free"]) / days * 7


def fmt_uptime(sec):
    if sec is None:
        return "?"
    d, rem = divmod(sec, 86400)
    h = rem // 3600
    return "%dd %dh" % (d, h) if d else "%dh %dm" % (h, (rem % 3600) // 60)


def render(s):
    sy, d, m, b = s["system"], s["disk"], s["memory"], s["battery"]
    macos = "macOS %s (%s)" % (sy["macos"], sy["build"]) if sy["macos"] else ""
    cpu = known(sy["chip"], "%d cores" % sy["cores"] if sy["cores"] else "", sep=", ")
    print("◆ " + (known(sy["model"], macos, cpu) or "this Mac"))
    print("  disk     %s free of %s (%s%%)" % (core.human_bytes(d["free"]), core.human_bytes(d["total"]), d["free_pct"]))
    used = core.human_bytes(m["used"]) + " used" + (" of " + core.human_bytes(m["total"]) if m["total"] else "")
    swap = "swap " + core.human_bytes(m["swap_used"]) if m["swap_used"] is not None else ""
    print("  memory   " + known(used, swap))
    if b:
        extra = ""
        if "health_pct" in b:
            extra = " · health %d%%, %d cycles" % (b["health_pct"], b.get("cycles", 0))
        print("  battery  %d%% %s on %s%s" % (b["percent"], b["state"], b["source"], extra))
    if s["uptime"] is not None or s["load"]:
        print("  uptime   %s · load %s" % (fmt_uptime(s["uptime"]), " ".join("%.2f" % x for x in s["load"]) or "?"))
    sec = s.get("security") or {}
    if sec:
        flag = lambda v: "on" if v else ("off" if v is False else "?")
        print("  security FileVault %s · SIP %s · firewall %s · Gatekeeper %s" % (
            flag(sec.get("filevault")), flag(sec.get("sip")), flag(sec.get("firewall")), flag(sec.get("gatekeeper"))))
    top = s.get("top") or {}
    if top.get("cpu"):
        print("  top cpu  " + ", ".join(_proc(p, "%.0f%%" % p["cpu"]) for p in top["cpu"][:4]))
    if top.get("memory"):
        # a group's resident memory is a sum, so pages its processes share count more than once
        print("  top mem  " + ", ".join(_proc(p, ("≈" if p.get("count", 1) > 1 else "") + core.human_bytes(p["rss"]))
                                         for p in top["memory"][:4]))
    week = disk_trend(trends())
    if week is not None:
        print("  trend    free disk %s%s per week" % ("+" if week >= 0 else "−", core.human_bytes(abs(week))))
    if s["signals"]:
        print()
        for sig in s["signals"]:
            print("  %s %s" % ("⚠" if sig["level"] == "warn" else "·", sig["msg"]))
    else:
        print("\n  all quiet.")


def main(args):
    refresh_backup(max_age=0)  # asked for by the human, so fresh (tmutil answers in milliseconds)
    s = collect()
    core.cache_put("status", s)
    core.emit(s, args.json, render)
    return 0
