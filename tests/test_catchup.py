import io
import os
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helpers import TempHome

from cos import core, evolve, shell, updates

GITHUB = {"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}
PERSON = {"GIT_AUTHOR_NAME": "A Person", "GIT_AUTHOR_EMAIL": "person@example.com",
          "GIT_COMMITTER_NAME": "A Person", "GIT_COMMITTER_EMAIL": "person@example.com"}


class Repos(TempHome):
    """A bare `origin`, the live checkout cloned from it, and a second clone standing in for
    GitHub's merge button. origin isn't GitHub, so that one check is stubbed out here and
    tested on its own."""
    stub_origin = True

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.live = os.path.join(self.tmp, "live")
        self.hub = os.path.join(self.tmp, "hub")
        self.sh("git", "init", "--quiet", "--bare", "-b", "main", self.origin)
        self.sh("git", "clone", "--quiet", self.origin, self.hub)
        self.merge("Start (#1)", "README.md")
        self.sh("git", "clone", "--quiet", self.origin, self.live)
        if self.stub_origin:
            stub = mock.patch.object(evolve, "_origin_problem", return_value=None)
            stub.start()
            self.addCleanup(stub.stop)

    def sh(self, *cmd, cwd=None, env=None):
        full = dict(os.environ, **PERSON)
        full.update(env or {})
        r = subprocess.run(cmd, cwd=cwd, env=full, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def merge(self, subject, path, text="x\n", committer=GITHUB, mode=None):
        """A commit on main the way GitHub's squash merge makes one, pushed to origin."""
        full = os.path.join(self.hub, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        if mode:
            os.chmod(full, mode)
        self.sh("git", "add", "-A", cwd=self.hub)
        self.sh("git", "commit", "--quiet", "-m", subject, cwd=self.hub, env=committer)
        self.sh("git", "push", "--quiet", "origin", "main", cwd=self.hub)

    def head(self):
        return self.sh("git", "rev-parse", "HEAD", cwd=self.live)

    def catch_up(self, **kw):
        kw.setdefault("fetch", "force")
        return evolve.catch_up(cwd=self.live, **kw)


class TestCatchUp(Repos):
    def test_it_reports_what_merged_without_moving_anything(self):
        before = self.head()
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.merge("Fix the banner (#43)", "lib/cos/shell.py")
        c = self.catch_up()
        self.assertEqual((c["behind"], c["ahead"], c["why"], c["updated"]), (2, 0, None, False))
        self.assertEqual(c["subjects"], ["Fix the banner (#43)", "cos net (#42)"])
        self.assertEqual(self.head(), before)
        self.assertEqual(core.cache_get("catch-up")["data"]["behind"], 2, "the boot banner reads this")

    def test_it_fast_forwards_a_clean_main_and_journals_the_way_back(self):
        before = self.head()
        self.merge("cos net (#42)", "lib/cos/net.py")
        c = self.catch_up(apply=True)
        self.assertTrue(c["updated"])
        self.assertEqual(self.head(), self.sh("git", "rev-parse", "HEAD", cwd=self.hub))
        entry = core.read_journal(days=1)[-1]
        self.assertEqual(entry["undo"], "git reset --keep %s" % before)
        self.assertEqual(entry["subjects"], ["cos net (#42)"])
        self.assertEqual(self.catch_up()["behind"], 0)

    def test_it_never_moves_a_dirty_tree_another_branch_or_local_commits(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        with open(os.path.join(self.live, "README.md"), "a") as f:
            f.write("an edit in progress\n")
        self.assertIn("uncommitted", self.catch_up(apply=True)["why"])
        self.sh("git", "checkout", "--quiet", "--", "README.md", cwd=self.live)

        self.sh("git", "checkout", "--quiet", "-b", "mine", cwd=self.live)
        self.assertIn("isn't on main", self.catch_up(apply=True)["why"])
        self.sh("git", "checkout", "--quiet", "main", cwd=self.live)

        with open(os.path.join(self.live, "notes.md"), "w") as f:
            f.write("local\n")
        self.sh("git", "add", "notes.md", cwd=self.live)
        self.sh("git", "commit", "--quiet", "-m", "local work", cwd=self.live)
        before = self.head()
        c = self.catch_up(apply=True)
        self.assertEqual((c["ahead"], c["behind"]), (1, 1))
        self.assertIn("1 commit GitHub hasn't got", c["why"])
        self.assertEqual(self.head(), before, "never rebased, never reset")

    def test_not_over_a_commit_pushed_straight_to_main(self):
        self.merge("pushed straight to main", "lib/cos/x.py")
        c = self.catch_up(apply=True)
        self.assertFalse(c["updated"])
        self.assertIn("didn't come from a GitHub pull request merge", c["why"])

    def test_not_over_a_commit_github_didnt_make(self):
        self.merge("Looks merged (#44)", "lib/cos/y.py", committer=PERSON)
        c = self.catch_up(apply=True)
        self.assertFalse(c["updated"])
        self.assertIn("didn't come from a GitHub pull request merge", c["why"])

    def test_it_names_the_gate_when_it_changes(self):
        self.merge("Tighten the hook (#45)", "lib/cos/hook.py")
        self.merge("Docs (#46)", "docs/x.md")
        c = self.catch_up()
        self.assertEqual(c["protected"], ["lib/cos/hook.py"])
        with mock.patch.object(evolve, "catch_up", return_value=c):
            line = shell._catch_up_line()
        self.assertIn("2 changes merged on GitHub", line)
        self.assertIn("the gate changes too: lib/cos/hook.py", line)
        self.assertIn("cos update apply claude-os --yes", line)

    def test_nothing_from_the_incoming_tree_runs(self):
        # The live checkout uses core.hooksPath=githooks (relative), so a merged
        # githooks/post-merge would run on the fast-forward if git were allowed hooks.
        self.sh("git", "config", "core.hooksPath", "githooks", cwd=self.live)
        marker = os.path.join(self.tmp, "hook-ran")
        self.merge("A hook (#47)", "githooks/post-merge", "#!/bin/sh\ntouch %s\n" % marker, mode=0o755)
        self.assertTrue(self.catch_up(apply=True)["updated"])
        self.assertFalse(os.path.exists(marker))

    def test_the_fetch_is_throttled_hardened_and_its_failure_isnt_fatal(self):
        real, fetches = core.run, []

        def spy(cmd, **kw):
            if "fetch" in cmd:
                fetches.append((cmd, kw))
            return real(cmd, **kw)
        self.merge("cos net (#42)", "lib/cos/net.py")
        with mock.patch.object(core, "run", spy):
            self.assertEqual(self.catch_up(fetch=True)["behind"], 1)
            self.catch_up(fetch=True)
            self.catch_up(fetch=False)
        self.assertEqual(len(fetches), 1, "at most one fetch every 10 minutes")
        cmd, kw = fetches[0]
        for flag in ("core.hooksPath=/dev/null", "credential.helper=", "--no-tags", "--no-recurse-submodules"):
            self.assertIn(flag, cmd)
        self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertLessEqual(kw["timeout"], 4)

        self.sh("git", "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"), cwd=self.live)
        c = self.catch_up()
        self.assertFalse(c["fetched"])
        self.assertEqual(c["behind"], 1, "what it fetched before still counts")

    def test_a_background_job_never_moves_the_code(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        os.environ["CLAUDE_OS_DAEMON"] = "pulse"
        try:
            c = self.catch_up(apply=True)
        finally:
            del os.environ["CLAUDE_OS_DAEMON"]
        self.assertFalse(c["updated"])
        self.assertIn("background job", c["why"])


class TestKeepUpToDate(Repos):
    def switch(self, on=True, url=None):
        core.config_set("autonomy.catch_up", on)
        core.config_set("autonomy.catch_up_url", url or self.origin)

    def test_off_by_default_and_then_nothing_moves(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.assertFalse(core.config()["autonomy"]["catch_up"])
        self.assertIsNone(evolve.auto_catch_up("pulse", cwd=self.live))

    def test_on_it_moves_forward_and_the_next_banner_says_so_once(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.switch()
        self.assertTrue(evolve.auto_catch_up("pulse", cwd=self.live)["updated"])
        self.assertEqual(self.head(), self.sh("git", "rev-parse", "HEAD", cwd=self.hub))
        line = shell._caught_up_line()
        self.assertIn("caught up with main (pulse", line)
        self.assertIn("cos net (#42)", line)
        self.assertIsNone(shell._caught_up_line(), "once")

    def test_only_for_the_repository_it_was_turned_on_for(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.switch(url="https://github.com/someone-else/claude-os.git")
        c = evolve.auto_catch_up("boot", cwd=self.live)
        self.assertFalse(c["updated"])
        self.assertIn("isn't the repository", c["why"])

    def test_pulse_moves_forward_only_while_the_app_is_closed(self):
        from cos import daemon
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.switch()
        real = evolve.catch_up
        with mock.patch.object(evolve, "catch_up", lambda **kw: real(**dict(kw, cwd=self.live))):
            with mock.patch.object(core, "app_running", lambda name, **kw: name == "ClaudeOS"):
                c = daemon.pulse_catch_up()
            self.assertEqual((c["behind"], c["updated"]), (1, False), "the app's server would mix versions")
            with mock.patch.object(core, "app_running", lambda name, **kw: False), \
                    mock.patch.object(evolve, "git", lambda *a, **kw: evolve.core.run(
                        ["git"] + list(a), cwd=self.live, timeout=kw.get("timeout", 60))):
                self.assertTrue(daemon.pulse_catch_up()["updated"])
        self.assertEqual(self.head(), self.sh("git", "rev-parse", "HEAD", cwd=self.hub))

    def test_the_server_catches_up_before_it_loads_and_starts_again(self):
        from cos import cli
        self.switch()

        class Restarted(Exception):
            pass

        def execv(path, argv):
            raise Restarted(argv)
        with mock.patch.object(evolve, "auto_catch_up", return_value={"updated": True}), \
                mock.patch.object(cli.os, "execv", execv), self.assertRaises(Restarted) as cm:
            cli.main(["serve", "--port", "0", "--exit-on-stdin-eof"])
        self.assertEqual(cm.exception.args[0][1:], [str(core.ROOT / "bin" / "cos"), "serve", "--port", "0",
                                                    "--exit-on-stdin-eof"])
        self.assertEqual(os.environ.pop("COS_CAUGHT_UP"), "1", "the new process doesn't catch up again")

    def test_only_the_apps_dialog_turns_it_on(self):
        from cos import cli
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["config", "autonomy.catch_up", "true"])
        self.assertEqual(cm.exception.code, 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["config", "autonomy.catch_up", "false"]), 0, "off is always fine")
        # Night shift is gone (0024): its old key isn't a switch cos config sets either way.
        for value in ("true", "false"):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main(["config", "autonomy.night_shift", value])
            self.assertEqual(cm.exception.code, 2, value)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["config", "autonomy.night_shift", "false"])


class TestReviewFindings(Repos):
    """An independent review of 0011's code (night of 26 October): each finding, kept fixed."""

    def switch(self):
        core.config_set("autonomy.catch_up", True)
        core.config_set("autonomy.catch_up_url", self.origin)

    def test_a_catch_up_that_cant_happen_isnt_offered(self):
        """Its why reaches the cache the desktop's Update item reads, or Update would offer it
        forever (the board on friction #21)."""
        core.config_set("autonomy.catch_up", True)
        core.config_set("autonomy.catch_up_url", "https://github.com/someone/else.git")
        self.merge("Feature (#2)", "feature.txt")
        c = evolve.auto_catch_up("pulse", cwd=self.live)
        self.assertFalse(c["updated"])
        self.assertIn("isn't the repository", c["why"])
        self.assertIn("isn't the repository", core.cache_get("catch-up")["data"]["why"])

    def test_cos_config_can_only_turn_a_switch_off(self):
        from cos import cli
        for argv in (["config", "autonomy", '{"catch_up": true, "night_shift": true}'], ["config", "autonomy", "false"],
                     ["config", "autonomy.catch_up_url", "https://github.com/x/y.git"], ["config", "autonomy.catch_up", "1"]):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)
        self.assertIsInstance(core.config()["autonomy"], dict)
        core.config_set("autonomy", False)  # however it got there, nothing crashes on it
        self.assertIsNone(evolve.auto_catch_up("boot", cwd=self.live))

    def test_only_the_apps_own_server_catches_up_and_never_under_another_server(self):
        from cos import cli
        self.switch()
        calls = []
        with mock.patch.object(evolve, "auto_catch_up", lambda *a, **k: calls.append(a) or {"updated": False}), \
                mock.patch("cos.server.main", lambda args: args.serving.close() or 0):
            cli.main(["serve", "--port", "0"])  # a browser's server
            os.environ["CLAUDE_OS_APP"] = "1"
            try:
                cli.main(["serve", "--port", "0", "--exit-on-stdin-eof"])  # started from an app chat
            finally:
                del os.environ["CLAUDE_OS_APP"]
            self.assertEqual(calls, [])
            other = evolve.hold_serving()
            try:
                self.assertTrue(evolve.serving())
                cli.main(["serve", "--port", "0", "--exit-on-stdin-eof"])
                self.assertEqual(calls, [], "never under another running server")
            finally:
                other.close()
            self.assertFalse(evolve.serving())
            cli.main(["serve", "--port", "0", "--exit-on-stdin-eof"])
            self.assertEqual(calls, [("app start",)])

    def test_boot_and_pulse_dont_move_code_under_a_browser_server(self):
        from cos import daemon
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.switch()
        held = evolve.hold_serving()
        try:
            real = evolve.catch_up
            with mock.patch.object(evolve, "catch_up", lambda **kw: real(**dict(kw, cwd=self.live))), \
                    mock.patch.object(core, "app_running", lambda name, **kw: False):
                self.assertFalse(daemon.pulse_catch_up()["updated"])
        finally:
            held.close()

    def test_what_is_merged_is_what_was_checked(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        checked = self.sh("git", "rev-parse", "HEAD", cwd=self.hub)
        real = evolve._fast_forward

        def meanwhile(out, cwd, source):
            # another process fetches a commit nobody checked, between the checks and the merge
            self.merge("pushed straight to main", "lib/cos/x.py")
            self.sh("git", "fetch", "--quiet", "origin", cwd=self.live)
            real(out, cwd, source)
        with mock.patch.object(evolve, "_fast_forward", meanwhile):
            self.assertTrue(self.catch_up(apply=True)["updated"])
        self.assertEqual(self.head(), checked)

    def test_a_move_that_failed_isnt_offered_again_until_asked(self):
        from cos import server
        self.merge("cos net (#42)", "lib/cos/net.py")
        with self._untracked("lib/cos/net.py") as f:  # an untracked file where the merge adds one
            f.write("in the way\n")
        c = self.catch_up(apply=True)
        self.assertFalse(c["updated"])
        self.assertIn("refused", c["why"])
        self.assertIn("refused", self.catch_up()["why"], "remembered for that commit")
        self.switch()
        app = server.App("t", head="0" * 40)
        app.managed = True
        with mock.patch.object(server.core, "run", lambda *a, **k: core.Result(0, "0" * 40, "")):
            self.assertEqual(server.code_state(app)["waiting"], 0, "no Update offered for a move that can't happen")
        self.assertIn("refused", self.catch_up(apply=True, retry=True)["why"], "the human can still try")

    def _untracked(self, path):
        os.makedirs(os.path.dirname(os.path.join(self.live, path)), exist_ok=True)
        return open(os.path.join(self.live, path), "w")

    def test_a_pgrep_that_fails_isnt_taken_for_no(self):
        with mock.patch.object(core, "run", return_value=core.Result(124, "", "timed out")):
            self.assertTrue(core.app_running("ClaudeOS", unknown=True))
            self.assertFalse(core.app_running("ClaudeOS"))
        with mock.patch.object(core, "run", return_value=core.Result(1, "", "")):
            self.assertFalse(core.app_running("ClaudeOS", unknown=True))

    def test_the_caught_up_line_survives_a_race_and_isnt_spent_by_a_daemon(self):
        core.cache_put("caught-up", {"subjects": ["cos net (#42)"], "protected": [], "source": "pulse", "head": "abc"})
        os.environ["CLAUDE_OS_DAEMON"] = "morning"
        try:
            self.assertIsNone(shell._caught_up_line())
        finally:
            del os.environ["CLAUDE_OS_DAEMON"]
        blob = core.cache_get("caught-up")
        core.home("cache", "caught-up.json").unlink()  # another session showed it first
        with mock.patch.object(core, "cache_get", return_value=blob):
            self.assertIn("caught up", shell._caught_up_line())

    def test_a_git_error_about_where_commits_came_from_isnt_safe(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        real = evolve.git

        def broken_log(*a, **kw):
            if a[:2] == ("log", "--first-parent"):
                return core.Result(128, "", "fatal")
            return real(*a, **kw)
        with mock.patch.object(evolve, "git", broken_log):
            c = self.catch_up(apply=True)
        self.assertFalse(c["updated"])
        self.assertIn("couldn't say where", c["why"])


class TestOrigin(Repos):
    stub_origin = False

    def test_only_github_and_no_url_rewrites(self):
        self.assertIn("isn't a GitHub repository", evolve._origin_problem(self.live))
        self.sh("git", "remote", "set-url", "origin", "https://github.com/example/claude-os.git", cwd=self.live)
        self.assertIsNone(evolve._origin_problem(self.live))
        self.sh("git", "config", "url.%s.insteadOf" % self.origin, "https://github.com/", cwd=self.live)
        self.assertIn("insteadOf", evolve._origin_problem(self.live))


class TestUpdateSource(Repos):
    def test_claude_os_is_an_update_source_and_apply_asks_first(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        real = evolve.catch_up
        with mock.patch.object(evolve, "catch_up", lambda **kw: real(cwd=self.live, **kw)):
            src = updates.check_claude_os()
            self.assertEqual(src["items"][0]["changes"], ["cos net (#42)"])
            with redirect_stdout(io.StringIO()) as out:
                updates.render_claude_os(src)
                self.assertEqual(updates.apply("claude-os", [], yes=False), 2)
            self.assertIn("1 change merged on GitHub", out.getvalue())
            self.assertNotEqual(self.head(), self.sh("git", "rev-parse", "HEAD", cwd=self.hub))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(updates.apply("claude-os", [], yes=True), 0)
            self.assertIn("✓ Ovenbird is at", out.getvalue())
            self.assertEqual(self.head(), self.sh("git", "rev-parse", "HEAD", cwd=self.hub))


class TestFollowTheApp(Repos):
    """Decision 0015, step 6: a downloaded copy's code follows its app. Opening a newer
    Ovenbird.app moves it to that app's commit (only forward, only over GitHub's merges,
    only a commit on main); it never follows main by itself."""

    def setUp(self):
        super().setUp()
        self.released = self.head()
        dl = mock.patch.object(core, "downloaded", return_value=True)
        dl.start()
        self.addCleanup(dl.stop)

    def app(self, commit, release=True, root=None):
        core.save_json(core.home("state", "app.json"),
                       {"release": release, "commit": commit, "version": "0.8.0", "root": root or self.live})

    def test_a_newer_app_brings_its_code(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        app_commit = self.sh("git", "rev-parse", "HEAD", cwd=self.hub)
        self.merge("Later on main (#43)", "lib/cos/later.py")
        self.app(app_commit)
        c = evolve.follow_app("app start", cwd=self.live)
        self.assertTrue(c["updated"], c["why"])
        self.assertEqual(self.head(), app_commit, "the app's commit, not main's tip")
        self.assertIsNone(evolve.follow_app("app start", cwd=self.live), "nothing more to follow")

    def test_never_back_and_never_off_main(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.assertTrue(self.catch_up(apply=True)["updated"])
        self.app(self.released)
        self.assertIsNone(evolve.follow_app("app start", cwd=self.live), "an older app doesn't move code back")
        self.sh("git", "checkout", "--quiet", "-b", "side", cwd=self.hub)
        self.merge("Not on main (#44)", "x.py")
        self.sh("git", "push", "--quiet", "origin", "side", cwd=self.hub)
        side = self.sh("git", "rev-parse", "HEAD", cwd=self.hub)
        self.sh("git", "fetch", "--quiet", "origin", "side", cwd=self.live)
        self.app(side)
        c = evolve.follow_app("app start", cwd=self.live)
        self.assertFalse(c["updated"])
        self.assertIn("isn't on origin/main", c["why"])
        self.assertIn("isn't on origin/main", evolve.follow_problem(), "and the person hears about it")

    def test_only_a_release_for_this_checkout(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        tip = self.sh("git", "rev-parse", "HEAD", cwd=self.hub)
        for record in ({"release": False}, {"root": os.path.join(self.tmp, "elsewhere")}):
            self.app(tip, **record)
            self.assertIsNone(evolve.follow_app("app start", cwd=self.live), record)
        self.app("abc1234")
        self.assertIsNone(evolve.follow_app("app start", cwd=self.live), "only a full commit")
        self.assertEqual(self.head(), self.released)

    def test_a_download_that_cant_follow_says_so(self):
        from cos import status
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.app(self.sh("git", "rev-parse", "HEAD", cwd=self.hub))
        with open(os.path.join(self.live, "README.md"), "w") as f:
            f.write("changed by hand\n")
        c = evolve.follow_app("app start", cwd=self.live)
        self.assertFalse(c["updated"])
        self.assertEqual(evolve.follow_problem(), "it has uncommitted changes")
        fake = {"disk": {"free": 10 ** 12, "free_pct": 50}, "memory": {"swap_used": 0}, "security": {}, "uptime": 0}
        self.assertIn("Ovenbird's code couldn't follow its app: it has uncommitted changes",
                      [m for _, m in status.signals(fake)])

    def test_no_false_alarm_for_a_download_at_its_apps_commit(self):
        from cos import daemon, status
        self.app(self.released)  # the app is at the checkout's own commit
        self.merge("cos net (#42)", "lib/cos/net.py")
        with open(os.path.join(self.live, "README.md"), "w") as f:
            f.write("changed by hand\n")
        self.assertIsNone(daemon.pulse_catch_up(), "pulse leaves a download to its app")
        evolve.catch_up(fetch=True, source="boot", cwd=self.live)  # a catch-up toward main, cached
        self.assertIsNone(evolve.follow_problem(), "only a failure to reach the app's commit counts")
        fake = {"disk": {"free": 10 ** 12, "free_pct": 50}, "memory": {"swap_used": 0}, "security": {}, "uptime": 0}
        self.assertFalse([m for _, m in status.signals(fake) if "couldn't follow" in m])
        core.cache_put("catch-up", "not a dict")
        self.assertIsNone(evolve.follow_problem())

    def test_update_apply_and_the_evolve_daemon_leave_a_download_alone(self):
        from cos import daemon
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            updates.apply_claude_os(yes=True)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(daemon.run("evolve"), 0)
        self.assertIn("doesn't run in a downloaded Ovenbird", out.getvalue())
        self.assertEqual(self.head(), self.released)

    def test_a_held_lock_says_so(self):
        self.merge("cos net (#42)", "lib/cos/net.py")
        tip = self.sh("git", "rev-parse", "HEAD", cwd=self.hub)
        with mock.patch.object(evolve, "_lock", return_value=None):  # another process holds it
            c = evolve.catch_up(apply=True, fetch=False, cwd=self.live, commit=tip)
        self.assertEqual(c["why"], "another catch-up is running")

    def test_a_download_never_follows_main_on_its_own(self):
        core.config_set("autonomy.catch_up", True)
        core.config_set("autonomy.catch_up_url", self.origin)
        self.merge("cos net (#42)", "lib/cos/net.py")
        self.assertIsNone(evolve.auto_catch_up("pulse", cwd=self.live))
        self.assertIsNone(updates.check_claude_os())
        self.assertIsNone(shell._catch_up_line())
        self.assertEqual(self.head(), self.released)


class TestNewerApp(TempHome):
    """A downloaded app says when a newer one is out, at most one look a day; one someone
    built never asks GitHub."""

    NEWER = {"version": "0.9.0", "url": "https://github.com/NobiResearch/Ovenbird/releases/tag/v0.9.0"}
    VITALS = {"disk": {"free": 10 ** 12, "free_pct": 50}, "memory": {"swap_used": 0}, "security": {}, "uptime": 0}

    def release(self, tag="v0.9.0", url="https://github.com/NobiResearch/Ovenbird/releases/tag/v0.9.0", **kw):
        body = dict({"tag_name": tag, "html_url": url, "draft": False}, **kw)

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        import json
        return mock.patch.object(core, "http_get", return_value=Reply(json.dumps(body).encode()))

    def app(self, **kw):
        core.save_json(core.home("state", "app.json"), dict({"release": True, "version": "0.8.0"}, **kw))

    def results(self):
        core.cache_put("updates", {"sources": {"brew": {"items": [{"name": "git"}], "error": None}}, "total": 1,
                                   "fetched": False, "missing_tools": []}, ts=1000.0)

    def yesterday(self):
        path = core.home("cache", "app-release.json")
        blob = core.load_json(path)
        core.save_json(path, dict(blob, ts=blob["ts"] - updates.LOOK_EVERY - 1))

    def looked(self, **latest):
        core.cache_put("app-release", {"latest": dict(self.NEWER, **latest)})

    def signal(self):
        from cos import status
        return [s for s in status.signals(self.VITALS) if "is out" in s[1]]

    def damaged_looks(self):
        now = time.time()
        return ("[1]", '"x"', "{", '{"ts": "x", "data": {"latest": null}}', '{"ts": null, "data": {}}',
                '{"data": {"latest": null}}', '{"ts": %f}' % now, '{"ts": %f, "data": "x"}' % now,
                '{"ts": %f, "data": {}}' % now, '{"ts": %f, "data": {"latest": "x"}}' % now,
                '{"ts": %f, "data": {"latest": {"version": "0.9.0", "url": "https://evil.example/"}}}' % now,
                '{"ts": %f, "data": {"latest": null}}' % (now + 10 * 365 * 86400),
                '{"ts": 1e400, "data": {"latest": null}}')

    def test_a_newer_release_is_one_quiet_line(self):
        self.app()
        with self.release():
            s = updates.check_app()
        self.assertEqual(s["items"][0]["latest"], "0.9.0")
        self.assertEqual(s["items"][0]["url"], "https://github.com/NobiResearch/Ovenbird/releases/tag/v0.9.0")
        with mock.patch.object(core, "http_get") as asked:
            self.assertEqual(updates.check_app()["items"][0]["latest"], "0.9.0")
        asked.assert_not_called()

    def test_the_same_or_older_or_odd_releases_say_nothing(self):
        self.app()
        for kw in ({"tag": "v0.8.0"}, {"tag": "v0.7.9"}, {"tag": "nightly"},
                   {"url": "https://evil.example/releases/tag/v0.9.0"}, {"draft": True}):
            core.home("cache", "app-release.json").unlink(missing_ok=True)
            with self.release(**kw):
                self.assertEqual(updates.check_app()["items"], [], kw)

    def test_offline_is_asked_again_tomorrow_not_now(self):
        self.app()
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
            self.assertEqual(updates.check_app()["items"], [])
        with mock.patch.object(core, "http_get") as asked:
            self.assertEqual(updates.check_app()["items"], [])
        asked.assert_not_called()

    def test_a_failed_look_keeps_what_the_last_one_found(self):
        self.app()
        with self.release():
            updates.check_app()
        self.yesterday()
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")) as asked:
            self.assertEqual(updates.check_app()["items"][0]["latest"], "0.9.0")
            self.assertEqual(updates.check_app()["items"][0]["latest"], "0.9.0")
        self.assertEqual(asked.call_count, 1)

    def test_two_looks_at_once_ask_once(self):
        def slow(*a, **kw):
            time.sleep(0.05)
            raise OSError("offline")
        with mock.patch.object(core, "http_get", side_effect=slow) as asked:
            looks = [threading.Thread(target=updates.latest_release) for _ in range(4)]
            for t in looks:
                t.start()
            for t in looks:
                t.join()
        self.assertEqual(asked.call_count, 1)

    def test_a_look_the_cache_cant_keep_still_counts(self):
        """A full disk: without this the desktop's polls would ask GitHub every minute."""
        self.app()
        self.addCleanup(updates.unrecorded.clear)
        with self.release():
            updates.check_app()
        self.yesterday()
        with mock.patch.object(core, "save_json", side_effect=OSError("disk full")), \
                mock.patch.object(core, "http_get", side_effect=OSError("offline")) as asked:
            for _ in range(3):
                self.assertEqual(updates.latest_release()["version"], "0.9.0")
        self.assertEqual(asked.call_count, 1)
        updates.unrecorded["ts"] -= updates.LOOK_EVERY + 1
        with self.release(tag="v0.9.1", url="https://github.com/NobiResearch/Ovenbird/releases/tag/v0.9.1"):
            self.assertEqual(updates.latest_release()["version"], "0.9.1")
        self.assertEqual(updates.unrecorded, {})  # the cache has it again, and goes on from there

    def test_a_damaged_record_counts_as_no_look(self):
        self.app()
        for body in self.damaged_looks():
            core.home("cache", "app-release.json").write_text(body)
            with self.release() as asked:
                self.assertEqual(updates.check_app()["items"][0]["latest"], "0.9.0", body)
            asked.assert_called_once()
            self.assertEqual(updates.last_look()["latest"]["version"], "0.9.0", body)

    def test_a_failed_look_doesnt_carry_damage_along(self):
        self.app()
        core.cache_put("app-release", {"latest": "x"}, ts=1000.0)
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
            self.assertEqual(updates.check_app()["items"], [])
        self.assertIsNone(core.cache_get("app-release")["data"]["latest"])

    def test_damaged_results_stay_as_they_are(self):
        self.app()
        with self.release():
            updates.check_app()
        for data in ({}, "x", {"sources": [1]}, {"sources": {"brew": {}}}):
            core.cache_put("updates", data, ts=1000.0)
            updates.fold_app()
            self.assertEqual(core.cache_get("updates"), {"ts": 1000.0, "data": data})

    def test_nothing_waits_for_a_look_under_way(self):
        self.app()
        with updates.LOOKING:
            fold = threading.Thread(target=updates.fold_app, daemon=True)
            fold.start()
            fold.join(5)
            self.assertFalse(fold.is_alive())

    def test_the_look_joins_old_results_without_freshening_them(self):
        self.app()
        self.results()
        with self.release():
            updates.fold_app()
        blob = core.cache_get("updates")
        self.assertEqual(blob["ts"], 1000.0)  # "checked 3 weeks ago" stays true, and pulse goes by it
        self.assertEqual(blob["data"]["total"], 2)
        self.assertEqual(blob["data"]["sources"]["app"]["items"][0]["latest"], "0.9.0")
        self.assertEqual(blob["data"]["sources"]["brew"]["items"], [{"name": "git"}])

    def test_a_poll_writes_only_when_the_line_changes(self):
        self.app()
        self.results()
        with self.release():
            updates.fold_app()
        with mock.patch.object(core, "cache_put") as put:
            updates.fold_app()
        put.assert_not_called()

    def test_the_line_leaves_with_the_new_app(self):
        self.app()
        self.results()
        with self.release():
            updates.fold_app()
        self.app(version="0.9.0")
        with mock.patch.object(core, "http_get") as asked:
            updates.fold_app()
        asked.assert_not_called()
        blob = core.cache_get("updates")
        self.assertEqual((blob["data"]["total"], blob["data"]["sources"]["app"]["items"]), (1, []))

    def test_no_results_yet_stay_none(self):
        """Half a cache would read as "macOS: up to date" in Updates, which checks all of it instead."""
        self.app()
        with self.release():
            updates.fold_app()
        self.assertIsNone(core.cache_get("updates"))

    def test_an_app_someone_built_never_asks(self):
        self.app(release=False)
        self.results()
        with mock.patch.object(core, "http_get") as asked:
            self.assertIsNone(updates.check_app())
            updates.fold_app()
        asked.assert_not_called()
        self.assertNotIn("app", core.cache_get("updates")["data"]["sources"])

    def test_code_newer_than_its_app_says_download(self):
        from cos import status
        self.app(version="0.6.0")
        older = core.app_outdated()
        self.assertEqual(older, {"have": "0.6.0", "needed": core.APP_NEEDED})
        self.app(version=core.APP_NEEDED)
        self.assertIsNone(core.app_outdated())
        self.app(release=False, version="0.1.0")
        self.assertIsNone(core.app_outdated(), "someone's own build")
        self.app(version="0.6.0")
        msgs = [m for _, m in status.signals(self.VITALS)]
        self.assertTrue(any("needs Ovenbird.app" in m and core.RELEASES in m for m in msgs), msgs)

    # The signal (decision 0026): for someone who never opens Updates.

    def test_someone_who_never_opened_updates_hears_of_it(self):
        self.app()
        self.looked()
        self.assertIsNone(core.cache_get("updates"))
        self.assertEqual(self.signal(), [("note", "Ovenbird.app 0.9.0 is out: see Updates")], "a note, never a warning")

    def test_the_name_stays_beside_the_count(self):
        """A count doesn't say one of them is what brings Ovenbird's own code."""
        from cos import status
        self.app()
        self.looked()
        self.results()
        self.assertEqual(len(self.signal()), 1)
        self.assertTrue(any("updates available" in m for _, m in status.signals(self.VITALS)), "and the count is there")

    def test_the_signal_never_makes_the_look(self):
        self.app()
        self.looked()
        self.yesterday()  # one is due, and it's the status poll's to make
        with mock.patch.object(core, "http_get") as asked:
            self.assertEqual(len(self.signal()), 1)
        asked.assert_not_called()

    def test_the_signal_doesnt_wait_for_a_look_under_way(self):
        self.app()
        self.looked()
        heard = []
        with updates.LOOKING:
            read = threading.Thread(target=lambda: heard.extend(self.signal()), daemon=True)
            read.start()
            read.join(5)
            self.assertFalse(read.is_alive())
        self.assertEqual(len(heard), 1)

    def test_the_signal_writes_nothing(self):
        self.app()
        self.looked()
        with mock.patch.object(core, "save_json") as save, mock.patch.object(core, "journal") as journal:
            self.assertEqual(len(self.signal()), 1)
        save.assert_not_called()
        journal.assert_not_called()

    def test_no_look_or_a_failed_one_says_nothing(self):
        self.app()
        self.assertEqual(self.signal(), [])
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
            updates.latest_release()
        self.assertEqual(self.signal(), [])

    def test_a_failed_look_keeps_the_line(self):
        self.app()
        self.looked()
        self.yesterday()
        with mock.patch.object(core, "http_get", side_effect=OSError("offline")):
            updates.latest_release()
        self.assertEqual(len(self.signal()), 1)

    def test_the_signal_and_updates_agree(self):
        self.looked()
        for version in ("0.8.0", "0.9.0", "0.9.1", "x", None):
            self.app(version=version)
            self.assertEqual(len(self.signal()), len(updates.check_app()["items"]), version)

    def test_the_signal_leaves_with_the_new_app(self):
        self.app()
        self.looked()
        self.assertEqual(len(self.signal()), 1)
        self.app(version="0.9.0")
        with mock.patch.object(core, "http_get") as asked:
            self.assertEqual(self.signal(), [])
        asked.assert_not_called()

    def test_a_copy_without_a_release_app_reads_no_look(self):
        self.looked()  # from a release app that someone's own build replaced
        for setup in (lambda: None, lambda: self.app(release=False, version="0.1.0")):
            setup()
            with mock.patch.object(updates, "last_look") as read:
                self.assertEqual(self.signal(), [])
            read.assert_not_called()
        self.assertIsNone(updates.newer_app({"version": "0.1.0"}, self.NEWER), "the rule says so too")

    def test_an_app_too_old_for_its_code_is_said_once(self):
        self.app(version="0.6.0")
        self.looked()
        self.assertEqual(self.signal(), [], "the warning already says to download it")

    def test_damage_is_no_line_and_no_exception(self):
        """signals() has no guard around it: one exception and every poll fails."""
        self.app()
        for body in self.damaged_looks():
            core.home("cache", "app-release.json").write_text(body)
            self.assertEqual(self.signal(), [], body)
        self.looked()
        for body in ("[1]", "{", '{"release": true}', '{"release": true, "version": "x"}',
                     '{"release": true, "version": 8}', '{"release": true, "version": null}'):
            core.home("state", "app.json").write_text(body)
            self.assertEqual(self.signal(), [], body)
        self.app()
        with mock.patch.object(updates, "last_look", side_effect=KeyError("ts")):
            self.assertEqual(self.signal(), [])

    def test_a_look_can_clear_what_the_signal_is_reading(self):
        self.addCleanup(updates.unrecorded.clear)
        updates.unrecorded.update(ts=time.time(), latest=self.NEWER)
        with mock.patch.object(core, "cache_get", side_effect=lambda *a, **kw: updates.unrecorded.clear()):
            self.assertEqual(updates.last_look()["latest"], self.NEWER)

    def test_only_plain_digits_reach_the_banner(self):
        self.app()
        for version in ("٠.٩.٠", "١.٠", "9" * 5000, "0.9.0\n", "0.9.0 and then some words", "0.9.0.1.2"):
            self.looked(version=version)
            self.assertIsNone(core.version_tuple(version), version)
            self.assertEqual(self.signal(), [], version)
        self.assertEqual(core.version_tuple("0.8.1"), (0, 8, 1))

    def test_pulse_never_notifies_about_it(self):
        from cos import clean, daemon, feeds, status
        self.app()
        self.looked()
        signals = [{"level": level, "msg": msg} for level, msg in status.signals(self.VITALS)]
        self.assertTrue(any("is out" in s["msg"] for s in signals), signals)
        s = dict(self.VITALS, signals=signals)
        with mock.patch.object(status, "collect", return_value=s), mock.patch.object(status, "refresh_backup"), \
                mock.patch.object(status, "record_trend"), mock.patch.object(feeds, "refresh"), \
                mock.patch.object(daemon, "pulse_catch_up"), mock.patch.object(updates, "check"), \
                mock.patch.object(clean, "scan"), mock.patch.object(core, "notify") as notify:
            daemon.pulse()
        notify.assert_not_called()


class TestDownloadsDontEvolve(TempHome):
    def test_no_local_merges_and_no_evolve_daemon(self):
        from cos import daemon
        with mock.patch.object(core, "downloaded", return_value=True), \
                mock.patch.object(daemon, "AGENTS_DIR", os.path.join(self.tmp, "LaunchAgents")):
            self.assertIn("follows its app", daemon.refusal("evolve"))
            self.assertFalse(daemon.install("evolve").ok)
            self.assertIsNone(daemon.refusal("pulse"))
            with mock.patch.object(evolve, "_check_branch"), self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    evolve.main(mock.Mock(evolve_cmd="merge", branch="evolve/x", yes=True, reviewed=False, json=False))


if __name__ == "__main__":
    unittest.main()
