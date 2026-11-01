import json
import os
import threading
import time
import unittest
from unittest import mock

from helpers import IN_SANDBOX, TempHome, local_connection, local_server

from cos import chat, core, server, updates


class TestRunFiles(unittest.TestCase):
    def test_minutes_without_the_chatter_before_them(self):
        from cos import server
        text, details = server.split_run("Board seats are deliberating.\n\nSteward is in.\n\n# Board minutes\n\n## Votes\n")
        self.assertTrue(text.startswith("# Board minutes"))
        self.assertIn("## Votes", text)
        self.assertEqual(details, "Board seats are deliberating.\n\nSteward is in.")
        self.assertEqual(server.split_run("# Good morning\n\nRain."), ("# Good morning\n\nRain.", ""))
        self.assertEqual(server.split_run("Fixed a thing\n\n---\nstderr:\nnoise")[0], "Fixed a thing")

    def test_one_cover_per_song(self):
        from unittest import mock
        from cos import server
        with mock.patch.object(server, "artwork_for", lambda track, artist: "https://art.example/%s.jpg" % track):
            own = server.with_artwork({"track": "A", "artist": "B", "artwork": "musicKit://artwork/transient/600x600?id=1"})
            app = server.with_artwork({"track": "A", "artist": "B"})
            self.assertEqual(own["artwork"], app["artwork"], "play/pause answers and polls agree")
            self.assertEqual(server.with_artwork({"track": "A", "artwork": "https://x/y.jpg"})["artwork"], "https://x/y.jpg")
            self.assertNotIn("artwork", server.with_artwork({"track": None}))


class TestChanges(TempHome):
    def test_pending_entries_show_under_unreleased_newest_first(self):
        import os
        from pathlib import Path
        from cos import server
        folder = Path(self.tmp) / "changes"
        folder.mkdir()
        (folder / "README.md").write_text("# not an entry")
        (folder / "2026-10-25-a.md").write_text("- older\n")
        (folder / "2026-10-26-b.md").write_text("- newer\n")
        text = server.with_changes("# Changelog\n\n## Unreleased\n\n- merged before\n\n## 0.7.0\n", folder)
        self.assertLess(text.index("- newer"), text.index("- older"))
        self.assertLess(text.index("- older"), text.index("- merged before"))
        self.assertNotIn("not an entry", text)
        self.assertEqual(server.with_changes("# Changelog\n", Path(self.tmp) / "none"), "# Changelog\n")
        self.assertIn("## Unreleased\n\n- newer", server.with_changes("# Changelog\n", folder))


class TestLocalServer(TempHome):
    # Evolve's test check (core.run_sandboxed) allows Unix sockets and no TCP at all, so this
    # fallback can't run there, and it's never needed there either.
    @unittest.skipIf(IN_SANDBOX, "needs TCP on 127.0.0.1; the evolve check's sandbox has no network (CI runs it)")
    def test_falls_back_to_localhost_where_unix_sockets_are_refused(self):
        """Claude Code's sandbox (the nightly evolve session's) refuses Unix sockets; the
        server tests still have to run there, or the run can't tell its failures apart."""
        import helpers
        app = server.App("t")
        sock = os.path.join(self.tmp, "refused.sock")
        with mock.patch.object(helpers, "UnixHTTPServer", side_effect=PermissionError(1, "Operation not permitted")):
            httpd = local_server(sock, server.make_handler(app))
        self.addCleanup(httpd.server_close)
        self.assertEqual(httpd.server_address[0], "127.0.0.1")
        self.assertFalse(os.path.exists(sock))
        app.port = 7357
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = local_connection(sock)
        conn.request("GET", "/api/status", headers={"Host": "127.0.0.1:7357", "X-COS-Token": "t"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


class TestServerSecurity(TempHome):
    def setUp(self):
        super().setUp()
        self.app = server.App("test-token")
        self.socket = os.path.join(self.tmp, "http.sock")
        self.httpd = local_server(self.socket, server.make_handler(self.app))
        self.app.port = 7357  # what the Host header has to name
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, method, path, body=None, token="test-token", host=None):
        conn = local_connection(self.socket)
        headers = {"Host": host or "127.0.0.1:%d" % self.app.port}
        if token:
            headers["X-COS-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body if isinstance(body, (str, bytes)) or body is None else json.dumps(body), headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, dict(res.getheaders()), data

    def test_api_needs_the_token(self):
        self.assertEqual(self.request("GET", "/api/friction", token=None)[0], 401)
        self.assertEqual(self.request("GET", "/api/friction", token="wrong")[0], 401)
        status, _, data = self.request("GET", "/api/friction")
        self.assertEqual((status, json.loads(data)), (200, []))

    def test_dns_rebinding_is_refused(self):
        self.assertEqual(self.request("GET", "/api/friction", host="evil.example:%d" % self.app.port)[0], 421)
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 421)

    def test_static_files_and_traversal(self):
        status, headers, data = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Ovenbird", data)
        self.assertIn("script-src 'self'", headers.get("Content-Security-Policy", ""))
        for bad in ("/../lib/cos/core.py", "/%2e%2e/lib/cos/core.py", "/..%2flib/cos/core.py"):
            self.assertEqual(self.request("GET", bad, token=None)[0], 404, bad)

    def test_writes_and_errors(self):
        status, _, data = self.request("POST", "/api/friction", {"text": "test friction"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["text"], "test friction")
        # Typed by the human in the Friction window: not a lead, and the list says so.
        self.assertNotIn("via", json.loads(data))
        self.assertIsNone(json.loads(self.request("GET", "/api/friction")[2])[-1]["lead"])
        # The desktop's warning pill reads `lead`: a marked item gets it from the server.
        from cos import mem
        mem.add_friction("from the chat", via="app")
        with mock.patch.dict(os.environ, {"CLAUDE_OS_DAEMON": "morning"}):
            mem.add_friction("from the morning run")
        leads = [f["lead"] for f in json.loads(self.request("GET", "/api/friction")[2])[-2:]]
        self.assertEqual(leads, ["the app's chat", "the morning run"])
        self.assertEqual(self.request("POST", "/api/friction", "{not json")[0], 400)
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/chat/0123456789ab/events")[0], 404)
        self.assertEqual(self.request("POST", "/api/clean/apply", {"ids": ["../../etc"]})[0], 400)
        self.assertEqual(self.request("POST", "/api/evolve/merge", {"branch": "main"})[0], 400)

    def test_the_merge_dialog_is_the_review(self):
        """The Evolve window's Merge dialog names a branch's safety files; the human's OK is
        their review (0013). Nothing else turns --reviewed on."""
        jobs = []
        self.app.job = lambda argv, label: jobs.append(argv) or {"id": "x"}
        sha = "a" * 40
        for path, body, flag in (("merge", {"reviewed": True, "tip": sha}, True), ("merge", {}, False),
                                 ("merge", {"reviewed": "yes", "tip": sha}, False),
                                 ("discard", {"reviewed": True, "tip": sha}, False)):
            self.assertEqual(self.request("POST", "/api/evolve/" + path, dict(body, branch="evolve/x"))[0], 200)
            self.assertEqual("--reviewed" in jobs[-1], flag, (path, body))
        self.assertEqual(jobs[0][-2:], ["--tip", sha], "the review is of the commit the dialog showed")
        self.assertNotIn("--tip", jobs[-1], "Discard isn't pinned")
        for body in ({"reviewed": True}, {"tip": "abc123"}, {"tip": sha.upper()}, {"tip": ["x"]}):
            self.assertEqual(self.request("POST", "/api/evolve/merge", dict(body, branch="evolve/x"))[0], 400, body)

    def test_the_desktop_leaves_music_alone_until_someone_clicks(self):
        """A new user with Music open: the poll and the Music window reopening show no macOS
        dialog. A click does, and from then on the desktop shows Music.app's track."""
        from unittest import mock
        from cos import core, music
        music._talked = False
        self.addCleanup(setattr, music, "_talked", False)
        played = core.Result(0, music.US.join(["playing", "Weightless", "Marconi Union", "", "480", "12", "40", "false", "off"]), "")
        with mock.patch.object(music, "own_player_active", return_value=None), \
                mock.patch.object(core, "app_running", return_value=True), \
                mock.patch.object(core, "osascript", return_value=played) as osa:
            self.assertEqual(json.loads(self.request("GET", "/api/music/now")[2]), {"running": False, "open": True})
            self.assertEqual(json.loads(self.request("GET", "/api/music/playlists")[2]), [])
            osa.assert_not_called()
            self.assertFalse(music.asked())
            self.assertEqual(self.request("POST", "/api/music/control", {"cmd": "next"})[0], 200)
            self.assertTrue(music.asked(), "a click is someone asking")
            osa.reset_mock()
            self.assertEqual(json.loads(self.request("GET", "/api/music/now")[2])["track"], "Weightless")
            osa.assert_called()

    def test_only_the_apps_server_says_it_runs_under_the_app(self):
        """What's started under it inherits this, so Music goes by the app's grant."""
        import io
        import os
        from contextlib import redirect_stdout
        from unittest import mock
        from cos import music, server
        for managed in (True, False):
            with mock.patch.object(server, "pin_code"), mock.patch.object(server.threading, "Thread"), \
                    mock.patch.object(server, "bind", return_value=mock.Mock(server_address=("127.0.0.1", 1))), \
                    mock.patch.dict(os.environ), redirect_stdout(io.StringIO()):
                os.environ.pop(music.UNDER_THE_APP, None)
                server.serve(exit_on_stdin_eof=managed)
                self.assertEqual(bool(os.environ.get(music.UNDER_THE_APP)), managed)

    def test_a_search_and_a_play_count_as_someone_asking(self):
        from unittest import mock
        from cos import core, music
        self.addCleanup(setattr, music, "_talked", False)
        for route, body in (("GET", "/api/music/search?q=x"), ("POST", "/api/music/play")):
            music._talked = False
            if music.asked():
                core.home(*music.ASKED_FILE).unlink()
            with mock.patch.object(music, "own_player_active", return_value=None), \
                    mock.patch.object(music, "player_enabled", return_value=False), \
                    mock.patch.object(music, "catalog_search", return_value={"results": []}), \
                    mock.patch.object(core, "app_running", return_value=True), \
                    mock.patch.object(core, "osascript", return_value=core.Result(0, "", "")):
                self.request(route, body, {"q": "x", "source": "library"} if route == "POST" else None)
            self.assertTrue(music.asked(), body)

    def test_someone_slow_on_macos_dialog_still_counts(self):
        """The click times out while the dialog waits, then they say yes, and the next call answers."""
        from unittest import mock
        from cos import core, music
        music._talked = False
        self.addCleanup(setattr, music, "_talked", False)
        now = core.Result(0, music.US.join(["playing", "Weightless", "Marconi Union", "", "480", "12", "40", "false", "off"]), "")
        with mock.patch.object(music, "own_player_active", return_value=None), \
                mock.patch.object(core, "app_running", return_value=True), \
                mock.patch.object(core, "osascript", side_effect=[core.Result(1, "", "timed out"), now]):
            self.request("POST", "/api/music/control", {"cmd": "next"})
        self.assertTrue(music.asked())

    def test_a_click_music_didnt_answer_asks_nothing_later(self):
        from unittest import mock
        from cos import core, music
        music._talked = False
        self.addCleanup(setattr, music, "_talked", False)
        with mock.patch.object(music, "own_player_active", return_value=None), \
                mock.patch.object(core, "app_running", return_value=True), \
                mock.patch.object(core, "osascript", return_value=core.Result(1, "", "not allowed")):
            self.request("POST", "/api/music/control", {"cmd": "next"})
            self.assertFalse(music.asked(), "macOS said no, or the click went nowhere")

    def test_radio_button_reaches_the_player_route(self):
        from cos import music
        orig = music.player_enabled
        music.player_enabled = lambda: False
        self.addCleanup(setattr, music, "player_enabled", orig)
        status, _, data = self.request("POST", "/api/music/play", {"radio": True})
        self.assertEqual(status, 200)
        self.assertIn("Apple Music", json.loads(data)["error"])

    def test_the_radio_chip_starts_what_music_code_picked(self):
        """Today's Radio chip: its label from station_seed, its click through radio(mine=True),
        and the journal's catalog id from what the player did, never from the request."""
        from unittest import mock
        from cos import core, music
        with mock.patch.object(music, "player_enabled", return_value=False):
            self.assertEqual(json.loads(self.request("GET", "/api/music/station")[2]), {})
        music.journal_event("radio", "radio from Song — Artist", catalog_id="456", label="Song — Artist")
        with mock.patch.object(music, "player_enabled", return_value=True):
            self.assertEqual(json.loads(self.request("GET", "/api/music/station")[2]), {"label": "Song — Artist"})
        played = {"ok": True, "source": "radio", "catalog_id": "456", "station": "Station",
                  "now": {"track": "Song", "artist": "Artist"}}
        with mock.patch.object(music, "radio", return_value=played) as radio:
            self.request("POST", "/api/music/play", {"radio": True, "mine": True, "catalog_id": "999", "name": "Planted"})
            self.request("POST", "/api/music/play", {"radio": True, "mine": "yes"})
        self.assertEqual([c.kwargs["mine"] for c in radio.call_args_list], [True, False])
        last = [e for e in core.read_journal(days=1, kind="music") if e.get("via") == "app"][-1]
        self.assertEqual((last["event"], last["catalog_id"], last["label"]), ("radio", "456", "Song — Artist"))

    def answer_dialog(self, approved, what):
        """Stand in for Ovenbird.app: when the confirm link opens, write the dialog's answer."""
        from urllib.parse import parse_qs, urlparse
        from cos import core
        real_run = core.run

        def fake_run(cmd, **kw):
            if cmd[:2] == ["open", "-g"] and cmd[2].startswith("claude-os://confirm/" + what):
                rid = parse_qs(urlparse(cmd[2]).query)["rid"][0]
                core.save_json(core.home("state", "confirm-%s.json" % what), {"rid": rid, "approved": approved})
                return core.Result(0, "", "")
            return real_run(cmd, **kw)
        return fake_run

    def test_there_is_no_night_shift_switch(self):
        # Decision 0024: nothing approves a push for Claude, so there's nothing to turn on.
        from unittest import mock
        from cos import core
        with mock.patch.object(server, "confirm_natively", lambda *a, **k: self.fail("never asks")):
            self.assertEqual(self.request("POST", "/api/settings/night", {"on": True})[0], 404)
        with mock.patch.object(server.core, "downloaded", lambda: True):
            settings = json.loads(self.request("GET", "/api/settings")[2])
        self.assertTrue(settings["downloaded"])
        self.assertNotIn("night_shift", settings)
        self.assertNotIn("night_shift", core.config()["autonomy"])

    def test_keeping_up_to_date_turns_on_only_in_the_dialog_and_pins_the_repository(self):
        from unittest import mock
        from cos import core
        core.home("state", "app.nonce").write_text("n0nce")
        with mock.patch.object(server.core, "app_running", lambda name: name == "ClaudeOS"):
            with mock.patch.object(server.core, "run", self.answer_dialog(False, "catch-up")):
                reply = json.loads(self.request("POST", "/api/settings/catch-up", {"on": True})[2])
            self.assertFalse(reply["catch_up"])
            self.assertFalse(core.config()["autonomy"]["catch_up"])
            with mock.patch.object(server.core, "run", self.answer_dialog(True, "catch-up")):
                reply = json.loads(self.request("POST", "/api/settings/catch-up", {"on": True})[2])
            self.assertTrue(reply["catch_up"])
        auto = core.config()["autonomy"]
        self.assertTrue(auto["catch_up"])
        url = core.run(["git", "-C", str(core.ROOT), "config", "--get", "remote.origin.url"]).out
        self.assertEqual(auto["catch_up_url"], url, "pinned to the origin it was turned on for")
        self.assertIs(json.loads(self.request("GET", "/api/settings")[2])["catch_up"], True)
        self.assertFalse(json.loads(self.request("POST", "/api/settings/catch-up", {"on": False})[2])["catch_up"])

    def test_the_update_item_and_restart_are_only_for_the_app(self):
        from unittest import mock
        from cos import core
        app = server.App("t")
        self.assertFalse(server.code_state(app)["restart"], "a browser's server can't be restarted for it")
        app.managed = True
        self.assertFalse(server.code_state(app)["restart"], "nothing changed")
        app.head = "0000000"
        self.assertTrue(server.code_state(app)["restart"], "the checkout moved under it")
        status, _, data = self.request("POST", "/api/restart", {})
        self.assertEqual(status, 400)
        exits = []
        self.app.managed = True
        with mock.patch.object(server.os, "_exit", exits.append):
            status, _, data = self.request("POST", "/api/restart", {})
            deadline = time.time() + 3
            while not exits and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual((status, exits), (200, [server.RESTART]))
        self.assertIn("restarted to update", core.read_journal(days=1)[-1]["msg"])

    def test_update_waits_for_a_running_job(self):
        """A job's output comes through the server: restarting would cut a cleanup off halfway."""
        from unittest import mock

        class Running:
            label, ended = "Clean up caches", None
        self.app.managed = True
        self.app.jobs["j"] = Running()
        exits = []
        with mock.patch.object(server.os, "_exit", exits.append):
            status, _, data = self.request("POST", "/api/restart", {})
            time.sleep(0.5)
        self.assertEqual(status, 409)
        self.assertIn("Clean up caches", json.loads(data)["error"])
        self.assertEqual(exits, [])
        Running.ended = time.time()
        with mock.patch.object(server.os, "_exit", exits.append):
            self.assertEqual(self.request("POST", "/api/restart", {})[0], 200)
            deadline = time.time() + 3
            while not exits and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(exits, [server.RESTART])

    def test_nothing_starts_while_it_restarts(self):
        """Closing the chats takes a while; a job started then would be cut off by the exit."""
        from unittest import mock
        self.app.managed = True
        closing, release, exits = threading.Event(), threading.Event(), []

        def slow_close():
            closing.set()
            release.wait(3)
        with mock.patch.object(self.app.chats, "close_all", slow_close), \
                mock.patch.object(server.os, "_exit", exits.append):
            self.assertEqual(self.request("POST", "/api/restart", {})[0], 200)
            self.assertTrue(closing.wait(3))
            with self.assertRaises(server.Restarting):
                self.app.job(["version"], "Late")
            status, _, data = self.request("POST", "/api/friction", {"text": "late"})
            self.assertEqual(status, 503)
            self.assertIn("restarting", json.loads(data)["error"])
            self.assertEqual(self.request("GET", "/api/friction")[0], 200, "reads still work")
            release.set()
            deadline = time.time() + 3
            while not exits and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(exits, [server.RESTART])
        self.assertEqual(self.app.jobs, {})

    def test_only_plain_files_are_served(self):
        """ui/ isn't a safety path: a symlink to /dev/zero or a FIFO in it must not hang the
        server before it reports ready, and nothing huge is held in memory."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            Path(d, "os.js").write_text("ok")
            os.symlink("/dev/zero", os.path.join(d, "zero.js"))
            os.symlink(os.path.join(d, "os.js"), os.path.join(d, "alias.js"))
            os.mkfifo(os.path.join(d, "pipe.js"))
            with open(os.path.join(d, "huge.js"), "wb") as f:
                f.truncate(server.UI_FILE_MAX + 1)
            os.symlink("/etc", os.path.join(d, "etc"))
            started = time.time()
            files = server.read_ui(d)
        self.assertEqual(files, {"os.js": b"ok"})
        self.assertLess(time.time() - started, 2)

    def test_the_desktop_folder_is_read_within_bounds(self):
        """ui/ made a link to the home folder, or holding far more than a desktop does, is
        never read in full before the server reports ready."""
        import tempfile
        from unittest import mock
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            real = Path(d, "real")
            real.mkdir()
            for i in range(5):
                Path(real, "f%d.js" % i).write_text("x" * 10)
            os.symlink(str(real), os.path.join(d, "ui"))
            self.assertEqual(server.read_ui(os.path.join(d, "ui")), {}, "ui/ as a link is read as empty")
            with mock.patch.object(server, "UI_TOTAL_MAX", (3, 10 ** 9)):
                self.assertEqual(len(server.read_ui(str(real))), 3)
            with mock.patch.object(server, "UI_TOTAL_MAX", (10 ** 6, 25)):
                self.assertEqual(len(server.read_ui(str(real))), 3, "stops once 25 bytes are in")

    def test_the_server_keeps_the_code_it_started_with(self):
        """0011, part three: every module and desktop file is loaded at start, so a checkout
        that moves under a running server can't mix new code into it."""
        import pkgutil
        import sys
        import tempfile
        from pathlib import Path
        names = server.pin_code()
        everything = [m.name for m in pkgutil.iter_modules(sys.modules["cos"].__path__)]
        self.assertEqual(sorted(names), sorted(everything))
        self.assertTrue(all("cos." + n in sys.modules for n in everything))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "js").mkdir()
            Path(d, "js", "os.js").write_text("old")
            files = server.read_ui(d)
            Path(d, "js", "os.js").write_text("new")
        self.assertEqual(files, {"js/os.js": b"old"})
        self.app.ui = {"index.html": b"<p>as started</p>", "os.js": b"// as started"}
        status, _, body = self.request("GET", "/", token=None)
        self.assertEqual((status, body), (200, b"<p>as started</p>"))
        self.assertEqual(self.request("GET", "/os.js", token=None)[2], b"// as started")
        self.assertEqual(self.request("GET", "/apps.js", token=None)[0], 404, "only what it started with")

    def test_a_stale_or_forged_answer_doesnt_count(self):
        from unittest import mock
        from cos import core
        core.home("state", "app.nonce").write_text("n0nce")
        core.save_json(core.home("state", "confirm-catch-up.json"), {"rid": "old", "approved": True})

        def wrong_rid(cmd, **kw):
            core.save_json(core.home("state", "confirm-catch-up.json"), {"rid": "guessed", "approved": True})
            return core.Result(0, "", "")
        with mock.patch.object(server.core, "app_running", lambda name: True), \
                mock.patch.object(server.core, "run", wrong_rid):
            self.assertFalse(server.confirm_natively("catch-up", wait=1.2))

    def test_docs_are_whitelisted(self):
        self.assertEqual(self.request("GET", "/api/doc?name=roadmap")[0], 200)
        self.assertEqual(self.request("GET", "/api/doc?name=../CLAUDE")[0], 404)


class TestChatEvents(TempHome):
    def setUp(self):
        super().setUp()
        self.s = chat.Session(spawn=False)

    def types(self):
        return [e["type"] for e in self.s.events]

    def test_stream_is_normalized(self):
        h = self.s._handle
        h({"type": "system", "subtype": "init", "model": "claude-test", "session_id": "x"})
        h({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}})
        h({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"},
                                                         {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "bin/cos status"}}]}})
        h({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "ok"}]}]}})
        h({"type": "result", "total_cost_usd": 0.02, "duration_ms": 900, "is_error": False})
        self.assertEqual(self.types(), ["ready", "text_delta", "text", "tool_use", "tool_result", "result"])
        self.assertEqual(self.s.events[4]["content"], "ok")
        self.assertEqual(self.s.state, "idle")
        self.assertEqual([e["seq"] for e in self.s.events], [1, 2, 3, 4, 5, 6])

    def test_not_signed_in_says_what_to_do_once(self):
        # What Claude Code 2.1 sends when this Mac user never signed in (recorded).
        h = self.s._handle
        h({"type": "assistant", "error": "authentication_failed", "message": {
            "model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
        h({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
        self.assertEqual(self.types(), ["result"])
        self.assertIn("isn't signed in for this Mac user", self.s.events[0]["text"])
        self.assertIn("claude auth login", self.s.events[0]["text"])
        # Any other error still reads as Claude Code said it.
        h({"type": "result", "is_error": True, "result": "API Error: overloaded"})
        self.assertEqual(self.s.events[1]["text"], "API Error: overloaded")

    def test_other_sign_in_errors_read_as_claude_code_said(self):
        # Claude Code tags a wrong API key (and a network hiccup) "authentication_failed" too.
        h = self.s._handle
        said = "Invalid API key · Fix external API key"
        h({"type": "assistant", "error": "authentication_failed", "parent_tool_use_id": None,
           "message": {"model": "<synthetic>", "content": [{"type": "text", "text": said}]}})
        h({"type": "result", "is_error": True, "result": said})
        self.assertEqual(self.types(), ["text", "result"])
        self.assertEqual(self.s.events[1]["text"], said)

    def test_permission_round_trip_is_journaled(self):
        self.s._handle({"type": "control_request", "request_id": "r1", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "bin/cos clean apply logs --yes"}}})
        self.assertIn("r1", self.s.pending)
        self.assertEqual(self.s.events[-1]["type"], "permission")
        self.assertTrue(self.s.answer("r1", allow=False))
        self.assertFalse(self.s.answer("r1", allow=True), "answering twice must not work")
        self.assertEqual(self.s.events[-1], dict(self.s.events[-1], type="permission_resolved", allow=False))
        from cos import core
        approvals = core.read_journal(kind="approval")
        self.assertIn("denied: Bash bin/cos clean apply logs --yes", approvals[-1]["msg"])

    def test_wait_returns_only_new_events(self):
        self.s._emit("user", text="hi")
        self.s._emit("text", text="hello")
        self.assertEqual([e["text"] for e in self.s.wait(1, timeout=0)], ["hello"])
        self.assertEqual(self.s.wait(2, timeout=0.05), [])


if __name__ == "__main__":
    unittest.main()


class TestDemoMode(TempHome):
    def test_the_demo_counts_its_own_updates(self):
        """Screenshots show the count and the list side by side: they have to agree."""
        from cos import demo
        n = sum(len(src["items"]) for src in demo.UPDATES["sources"].values())
        self.assertEqual(demo.UPDATES["total"], n)
        self.assertIn("%d updates available" % n, demo.STATUS["signals"][0]["msg"])
        self.assertIn("**Updates:** %d waiting" % n, demo.BRIEF["text"])
        self.assertTrue(any("%d updates available" % n in line for line in demo.BOOT))

    def test_demo_trends_have_about_thirty_rows(self):
        """The Vitals sparklines need 30 daily rows to draw from."""
        from cos import demo
        rows = demo.GETS["/api/trends"]["rows"]
        self.assertEqual(len(rows), 30)
        self.assertEqual([r["date"] for r in rows], sorted(r["date"] for r in rows))
        for r in rows:
            self.assertIn("disk_free", r)
            self.assertIn("battery_health", r)
        self.assertTrue(all(rows[i]["disk_free"] > rows[i + 1]["disk_free"]
                            for i in range(len(rows) - 1)), "disk free falls over the month")
        self.assertEqual(rows[-1]["disk_free"], demo.STATUS["disk"]["free"])
        self.assertEqual(rows[-1]["battery_health"], demo.STATUS["battery"]["health_pct"])
        self.assertEqual(rows[-1]["battery_cycles"], demo.STATUS["battery"]["cycles"])

    def test_demo_never_reads_or_changes_the_real_machine(self):
        from cos import demo
        app = server.App("t", demo=True)
        sock = os.path.join(self.tmp, "demo.sock")
        httpd = local_server(sock, server.make_handler(app))
        app.port = 7357
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def call(method, path, body=None):
                conn = local_connection(sock)
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Host": "127.0.0.1:%d" % app.port, "X-COS-Token": "t", "Content-Type": "application/json"})
                res = conn.getresponse()
                return res.status, json.loads(res.read())
            self.assertEqual(call("GET", "/api/status")[1]["system"]["model"], "Mac16,8")
            self.assertEqual(call("POST", "/api/clean/apply", {"ids": ["logs"]})[1]["output"], "(demo mode: nothing was changed)")
            self.assertEqual(call("POST", "/api/friction", {"text": "x"})[1]["demo"], True)
            from cos import mem
            self.assertEqual(mem.frictions(), [], "demo writes must not reach ~/.claude-os")
            status, s = call("POST", "/api/chat", {"text": "do I need to clean up?"})
            self.assertEqual(status, 200)
            self.assertIsInstance(app.chats.get(s["id"]), demo.DemoSession)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestTheDaysLook(TempHome):
    """A downloaded app looks for a newer one by itself (0015, step 6): on the desktop's status
    poll and when Updates opens, and only the app's own server does."""

    NEWER = {"version": "0.9.0", "url": "https://github.com/NobiResearch/Ovenbird/releases/tag/v0.9.0"}

    def setUp(self):
        super().setUp()
        self.app = server.App("t")
        self.app.port = 7357
        self.app.managed = True
        self.socket = os.path.join(self.tmp, "http.sock")
        httpd = local_server(self.socket, server.make_handler(self.app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.downloaded(True)
        core.cache_put("updates", {"sources": {"brew": {"items": [{"name": "git"}], "error": None}}, "total": 1,
                                   "fetched": False, "missing_tools": []}, ts=1000.0)
        collect = mock.patch("cos.status.collect", return_value={"signals": []})
        collect.start()
        self.addCleanup(collect.stop)
        self.addCleanup(updates.unrecorded.clear)
        self.threads = threading.active_count()

    def downloaded(self, release):
        core.save_json(core.home("state", "app.json"), {"release": release, "version": "0.8.0"})

    def settle(self):
        """Wait for the looks the polls started."""
        deadline = time.time() + 5
        while threading.active_count() > self.threads and time.time() < deadline:
            time.sleep(0.01)

    def get(self, path):
        conn = local_connection(self.socket)
        conn.request("GET", path, headers={"Host": "127.0.0.1:7357", "X-COS-Token": "t"})
        res = conn.getresponse()
        data = json.loads(res.read())
        conn.close()
        return res.status, data

    def test_updates_shows_the_look_on_results_of_any_age(self):
        with mock.patch.object(updates, "latest_release", return_value=self.NEWER), \
                mock.patch.object(updates, "check", side_effect=AssertionError("checked the whole Mac")):
            status, blob = self.get("/api/updates")
        self.assertEqual(status, 200)
        self.assertEqual((blob["ts"], blob["data"]["total"]), (1000.0, 2))
        self.assertEqual(blob["data"]["sources"]["app"]["items"][0]["latest"], "0.9.0")

    def test_a_failed_look_changes_nothing_on_screen(self):
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
            status, blob = self.get("/api/updates")
        self.assertEqual(status, 200)
        self.assertEqual((blob["ts"], blob["data"]["total"]), (1000.0, 1))
        self.assertEqual(blob["data"]["sources"]["app"], {"items": [], "error": None})

    def test_the_status_poll_doesnt_wait_for_the_look(self):
        finish, done = threading.Event(), threading.Event()

        def look():
            finish.wait(5)
            done.set()
        with mock.patch.object(updates, "fold_app", side_effect=look):
            self.assertEqual(self.get("/api/status")[0], 200)
            self.assertFalse(done.is_set())
            finish.set()
            self.assertTrue(done.wait(5))

    def test_the_status_poll_asks_once_a_day(self):
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")) as asked:
            for _ in range(3):
                self.assertEqual(self.get("/api/status")[0], 200)
            self.settle()
        self.assertEqual(asked.call_count, 1)

    def test_the_status_poll_asks_once_a_day_on_a_full_disk(self):
        with mock.patch.object(core, "save_json", side_effect=OSError("disk full")), \
                mock.patch.object(core, "http_get", side_effect=OSError("offline")) as asked:
            for _ in range(3):
                self.assertEqual(self.get("/api/status")[0], 200)
            self.settle()
        self.assertEqual(asked.call_count, 1)

    def test_the_count_follows_the_app_on_the_next_poll(self):
        core.cache_put("app-release", {"latest": self.NEWER})  # today's look is done: only the app changes
        with mock.patch.object(core, "http_get") as asked:
            self.get("/api/status")
            self.settle()
            self.assertEqual(core.cache_get("updates")["data"]["total"], 2)
            core.save_json(core.home("state", "app.json"), {"release": True, "version": "0.9.0"})
            self.get("/api/status")
            self.settle()
        asked.assert_not_called()
        self.assertEqual(core.cache_get("updates")["data"]["total"], 1)

    def test_a_damaged_record_of_the_look_changes_nothing_on_screen(self):
        for body in ("[1]", '"x"', "{", '{"ts": "x", "data": {}}', '{"ts": null}', '{"ts": 1, "data": {"latest": "x"}}'):
            core.home("cache", "app-release.json").write_text(body)
            with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
                self.assertEqual(self.get("/api/status")[0], 200, body)
                self.settle()
                self.assertEqual(self.get("/api/updates")[0], 200, body)
            self.assertIsNone(core.load_json(core.home("cache", "app-release.json"))["data"]["latest"], body)

    def test_damaged_results_are_served_or_checked_again(self):
        core.cache_put("app-release", {"latest": self.NEWER})
        fresh = {"sources": {}, "total": 0, "fetched": False, "missing_tools": []}
        with mock.patch.object(updates, "check", return_value=fresh):
            for body in ('{"ts": 1, "data": {}}', '{"ts": 1, "data": "x"}', '{"ts": 1, "data": {"sources": [1]}}',
                         '{"ts": "x", "data": {}}', "[1]", "{"):
                core.home("cache", "updates.json").write_text(body)
                self.assertEqual(self.get("/api/status")[0], 200, body)
                self.settle()
                self.assertEqual(self.get("/api/updates")[0], 200, body)

    def test_a_server_the_app_didnt_start_never_looks(self):
        self.app.managed = False  # `cos serve`, on a Mac where a downloaded app once ran
        with mock.patch.object(updates, "fold_app") as fold:
            self.assertEqual(self.get("/api/status")[0], 200)
            self.assertEqual(self.get("/api/updates")[0], 200)
            self.settle()
        fold.assert_not_called()

    def test_an_app_someone_built_never_asks(self):
        self.downloaded(False)
        with mock.patch.object(core, "http_get") as asked:
            self.assertEqual(self.get("/api/status")[0], 200)
            self.assertEqual(self.get("/api/updates")[0], 200)
            self.settle()
        asked.assert_not_called()
