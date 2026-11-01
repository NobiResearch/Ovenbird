# 0015 — Claude OS for everyone: a signed, downloadable app

- **Status:** accepted with conditions; step by step, each waiting for the one before
- **Date:** 2026-10-26
- **Decided by:** the board, on the human's "yes, start it"

## Context

The human wants people who aren't developers to use Claude OS, with Apple Music as one click.
Today everyone builds Claude OS.app themselves, and its own Apple Music player (MusicKit) works
only for an app Apple can tie to a developer team. "Sign in with Apple" doesn't help: it's
identity, not Apple Music access. A person's own access is macOS's "Allow access to Apple Music"
prompt, which Claude OS already shows. What's missing is an app built and signed once, by the
project, that anyone can download. The human has a paid developer account with a personal
Developer ID Application certificate; their current provisioning profile is a Development
profile for one registered Mac.

## Votes

| Seat | Vote | Top reason |
|---|---|---|
| Architect | amend | A Developer ID app that clones the code is the smallest shape that works (catch-up, Update and CLAUDE.md all expect a git checkout). The hardened runtime needs entitlements for Calendar, Location and Apple Events; nothing may fall back to ad hoc. |
| Guardian | amend | Today's build would ship the home path, the builder's PATH and a Development profile (this Mac's hardware id, the team name) in every copy, and no scan covers build output. Publishing isn't gated, and the signature carries the human's legal name forever. |
| Steward | amend | It's a bet on future users, so the riskiest step goes first. For a non-developer the first launch is the whole experience: one checklist, nothing installed before a click, plain words. |
| Skeptic | amend | The premise is untested: if MusicKit refuses a Developer ID app, the promise is gone. A signed app is one hurdle of four (the command line tools, Claude Code, a sign-in, a subscription); keep the installer small. |

## Decision

0. **Gates first.**
   - `app/` is a safety path (`hook.PROTECTED`): its build, signing and setup code merges after a guardian review.
   - A daemon's model may not run `gh` or `gh-axi` (`ALWAYS_DENIED`). Code a daemon calls still reads GitHub (catch-up fetches, the brief lists pull requests), but nothing a daemon runs opens, merges or publishes anything (`cos evolve merge` is refused in daemons).
   - `ask` rules for `gh release*`, `gh-axi release*`, `gh api *releases*`, `gh-axi api *releases*` and `xcrun notarytool*` (rules that only add a prompt, so Claude could add them). A release is published with `gh release`, never another way. When step 3 lands, `app/build.sh --release*` and `./app/build.sh --release*` get `ask` rules too, since the notarization inside it never shows as a command of its own.
   - Releases are built only on the human's Mac, never in CI. The certificate and notary credentials never become GitHub secrets.
1. **Test that MusicKit works, before anything else is built.**
   - A Developer ID–signed, hardened-runtime build plays a catalog song through MusicKit after "Allow".
   - It uses a new neutral App ID with MusicKit, not one derived from the human's name, and its Developer ID provisioning profile.
   - It runs from `build/` against the existing checkout, with the human's own app quit, and goes to the Trash afterwards. Then `claude-os://` opens the human's app again. This test build is never notarized or uploaded: it's today's build, with the home path and PATH baked in.
   - The full check (notarized, a quarantined download, a second macOS user account; Calendar, Location, notifications and Music automation all prompt and work) comes before the first public release.
   - If MusicKit fails, stop and bring it back to the board.
2. **The human's name.** Before anything is public, the human sees exactly what a downloader sees ("Developer ID Application: <full name>") and gives an explicit yes, or waits for an organization account.
3. **`app/build.sh --release`.** `app/build.sh` without the flag behaves exactly as today.
   - It writes no `ClaudeOSRoot` or `ClaudeOSPath`, and bakes the release commit's SHA.
   - It strips the binary.
   - It takes only an explicitly named Developer ID profile (all devices, no device list) for the release App ID, whose `Name` and `AppIDName` are neutral. It checks the profile by parsing it.
   - Its entitlements come from an allowlist, never `get-task-allow`, `disable-library-validation`, `allow-jit` or the like.
   - It signs with `--options runtime --timestamp` and notarizes with a Keychain profile only.
   - It refuses under `CLAUDE_OS_DAEMON`, `COS_SANDBOXED` or `CI`, on a dirty tree, or off `origin/main`, and fails rather than falling back to ad hoc.
   - Before signing, it scans Info.plist, the binary (`strings`, `nm`), the resources and the embedded profile with the Guardian's patterns, and any hit stops it. (The profile and the signature carry the team's name by design. The profile is checked by parsing too, and the scan leaves out only that name. It first ran before the profile was embedded; Fable's review of 29 October moved it after.)
4. **The app finds its code without baked paths.** The order is: Info.plist (dev builds), `CLAUDE_OS_ROOT` (dev builds only), the `cos` link, then `~/Library/Application Support/Claude OS`. A release build checks what it found before running it (`bin/cos`, `lib/cos`, and an origin of `github.com/NobiResearch/Claude-OS`). PATH is a fixed list. The app records where it is in `~/.claude-os/state/app.json` for `cos` to read. First-launch setup code that lands outside `app/` (a new `lib/cos/` module, say) joins `hook.PROTECTED` too.
5. **First launch: one native checklist, nothing before a click.**
   - Command line tools: checked with `xcode-select -p`, which never prompts, before any `git` or `python3` shim runs. `xcode-select --install` only on a click.
   - Claude Code: a link to Anthropic's page or the command to paste. Never a script piped to a shell.
   - The clone: `github.com/NobiResearch/Claude-OS` at the release commit, with catch-up's git hardening, into an empty `~/Library/Application Support/Claude OS`.
   - Existing installs: adopt one or stop. Never re-point another checkout's daemons, links or hooks.
   - Opt-ins: `cos install`'s extras (skills in `~/.claude/skills`, the pulse daemon) are opt-ins that start off.
   - Downloaded copies get a `settings.local.json` that denies `gh` and `git push`. The night shift is hidden unless origin is the person's own repository.
   - "Remove Claude OS" undoes only what this copy added and moves the checkout to the Trash. `~/.claude-os` (the journal, memory) goes to the Trash only after its own yes.
   - It never asks for Full Disk Access, Accessibility or Screen Recording.
6. **Updates.**
   - No Sparkle and no app that replaces itself.
   - The app only says a newer release exists: a quiet line in Updates, at most once a day. (0026 adds one line among the signals.)
   - Keep up to date stays off by default. Downloaded copies follow the releases the human cuts, not raw main. If they ever follow main, the switch's dialog says plainly that Claude merges some changes without a human's review.
   - Python that gets ahead of an older app says "download Claude OS.app X" instead of breaking.
7. **Publishing.**
   - A GitHub Release only on the human's yes in the conversation, each time, never from a daemon.
   - The notes say, in plain words: what you need (macOS 14+, Claude Code and a Claude plan, Apple Music for music), what first launch adds and how to remove it, that it's MIT-licensed (binaries too; decision 0016), and that it isn't made or endorsed by Anthropic.
   - The notes pass `cos guard --text`.

## Step 1's result (26 October): passed

A test build signed with the human's Developer ID, with the hardened runtime, the new App ID
`app.claude-os.desktop` (MusicKit under App Services) and its Developer ID profile, played a
catalog song through its own MusicKit player. The first try failed with Apple's "not registered
as a valid client identifier" (404): the App ID was minutes old, and the app cached the failure
until it was relaunched. The test also showed why step 4 matters: with the human's app
installed, `cos` reached that app instead of the test build (`claude-os://`, `ensure_app`, the
shared nonce). The test build was never notarized or uploaded; it's in the Trash, and
`claude-os://` opens the human's app again. Still to come before a public release: the full
check in a second macOS user account (notarized, a quarantined download, Calendar, Location,
notifications and Music automation).

## Step 5: the checklist (27 October)

- A release that finds no Claude OS shows a native window (`SetupWindowController`, logic in
  `FirstLaunch`): the command line tools (`xcode-select -p`; `--install` on a click, then it
  polls), Claude Code (found in a fixed list of folders, or a link to Anthropic's page), and the
  download, enabled only once the tools are there.
- The download runs the developer folder's own git, never the /usr/bin shim, with catch-up's
  flags plus https only, and without the system's or the person's git config (a
  `url.insteadOf` there could redirect it). It clones main into a staging folder beside
  `~/Library/Application Support/Claude OS`, checks the release commit is on main, puts `main`
  on it tracking GitHub's, checks the result is Claude OS at that commit, turns on the
  Guardian's hooks (`githooks/`), gives origin a push address that goes nowhere, writes
  `.claude/settings.local.json` (no `gh`, `gh-axi` or `git … push`), and only then moves it
  into place. A folder that's already there and isn't empty (or is a link) is never touched;
  an empty one is removed with rmdir, so anything that appears meanwhile stops it. Quitting
  mid-download stops git and waits up to 10 seconds for the half copy to go; leftovers from a
  crash are removed at the next first launch, only if their origin is this repository.
- Someone who points a download at their own fork also clears `remote.origin.pushurl`.
- The opt-ins (the skills everywhere, pulse) are unticked boxes, applied through `cos` when the
  human opens Claude OS.
- `~/.claude-os/state/install.json` records the download. With it, and origin still this
  repository, `cos` hides Night shift and refuses it, and the night shift's hook never approves.
- `cos daemon install` and `uninstall` (and `cos install`/`uninstall`) leave alone a daemon
  that runs from another Claude OS checkout.
- `~/.claude-os` is created with mode 0700 when the app is first to make it.
- **Remove Claude OS…** (the app menu, only in a release, only when `install.json` names the
  folder first launch downloads into, a real folder and not a link, and the app runs it):
  after a yes, it stops the server, runs `cos uninstall` (this checkout's daemons, and links
  only if they're its own), and moves the checkout to the Trash. A failure at either step
  stops it with everything in place, so it can be tried again. Last of all, and only after
  its own yes, ~/.claude-os goes to the Trash; the dialog says conversations stay in Claude
  Code's own history. The app itself is dragged to the Trash by the person.

## Step 6: updates (27 October)

- **A downloaded copy's code follows its app.** When the app's server starts, before it
  loads anything, `evolve.follow_app` moves the checkout forward to the commit the app was
  built from: only forward, only a commit on GitHub's main, only over GitHub's squash merges,
  through catch-up's lock and guards, journaled with the way back. Downloading a newer
  release is the person's yes; nothing else moves a download's code. Keep Claude OS up to
  date, the boot banner's "merged on GitHub" line and the Updates window's Claude OS card are
  for checkouts that follow main, so a download doesn't show them.
- **A newer app is one quiet line.** For a downloaded app, Updates asks GitHub for the latest
  release at most once a day (whatever the answer, offline included) and, if it's newer,
  shows a card with a link to its release page. An app someone built never asks. Since 0026 the
  signals say so too, from the same look.
- **Code newer than its app says so.** `core.APP_NEEDED` is the oldest app the code works
  with; a downloaded app older than that gets a warning signal with the download link.
- **A download's main takes no local commits**, or the next app couldn't bring its code: `cos
  evolve merge` refuses there, and so do `cos daemon install evolve`, `cos daemon run evolve`
  and `cos update apply claude-os`.
- **A download that can't follow its app says why** (a hand edit, say): a warning signal from
  its last try to reach the app's commit. It forces a fetch only for a commit it hasn't seen,
  so the app's server starts fast, and pulse leaves a download to its app.
- The Keep up to date dialog now says plainly that some changes are merged by Claude without
  a person's review.

## Step 7's result (29 October): published, with part of step 1's full check on record

- v0.8.0 was built on the human's Mac with `app/build.sh --release`, notarized and stapled. The
  human saw the signature's name and said yes (step 2).
- The zip check on the first build found `._` files in the zip: macOS's provenance tags, and
  the provisioning profile's download address. A plain `unzip` leaves them inside the app and
  breaks its signature. #159 zips without extended attributes and stops the build if any are
  in it.
- The first-launch check ran twice from the draft release's zip, both times in a second macOS
  user account on the same Mac. The first run found two things:
  - the checklist found Claude Code, but it wasn't signed in for that user, so the chat's first
    answer was a dead end ("Please run /login", which only works in a terminal);
  - a checkbox that ran past the window's margin.

  #160 fixed both. The release was rebuilt from that commit and the draft's zip replaced. The
  second run passed on the human's word.
- **Not on record as checked** in either run (step 1's full check, docs/releasing.md):
  - the command line tools and Claude Code from nothing: on the same Mac both were already
    installed, so neither install path (`xcode-select --install`, the link to Anthropic's page)
    ran;
  - the zip's quarantine (`xattr -p`) and the cloned code's (`xattr -l` on `bin/cos`);
  - `cos` from Terminal and from Claude Code there;
  - Calendar, Location, notifications, Music automation and the Apple Music player;
  - Remove Ovenbird….

  The next release's first-launch check covers them. The install paths need a Mac without
  them.
- Published as the latest release, not as a pre-release, with immutable releases on. The
  README links `releases/latest` (#154). The notes say it has been tried on one Mac.

## Not now

An installer that sets up Claude Code itself, an organization developer account, and the App Store,
whose sandbox can't run what Claude OS does.
