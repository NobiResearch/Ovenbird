# Releasing Ovenbird.app

How the downloadable app is built (decision 0015). Only the repository's owner cuts a release,
on their own Mac; nothing here runs in CI, and publishing is a separate step on their yes.

## Once

1. **A Developer ID certificate** ("Developer ID Application") in your login Keychain.
2. **The release App ID** (`app.claude-os.desktop`, MusicKit ticked under App Services) and a
   **Developer ID provisioning profile** for it, named neutrally. Keep the profile outside the
   repository, for example in `~/.claude-os/`. A brand-new App ID can take a few minutes before
   MusicKit accepts it (see `docs/lessons.md`).
3. **Notarization credentials in the Keychain**: create an app-specific password for your Apple
   Account (account.apple.com → Sign-In and Security → App-Specific Passwords), then run, in your
   own Terminal:

   ```sh
   xcrun notarytool store-credentials claude-os --apple-id <your Apple Account> --team-id <your team id>
   ```

   It asks for the password and keeps it in the Keychain under the name `claude-os`. The password
   is never written to a file, an environment variable or the repository.

## Before the first public release

The full check from decision 0015, step 1, on the notarized zip, before it's published (the
draft release's zip, see Publishing): download it in a **second macOS user account**, in Safari
signed in to GitHub (a draft's files download only for people with write access), so it's
quarantined and nothing of yours is there. Before opening it, `xattr -p com.apple.quarantine`
on the zip (or on the app, if Safari unzipped it) should print a value: a copy moved over by
`gh` or a shared folder isn't quarantined and proves nothing. Then go through the first launch
from nothing. The command line tools, Claude Code and the download, then Calendar,
Location, notifications, Music automation and the Apple Music player all prompt and work, and
Remove Ovenbird… leaves nothing behind but the app and the permissions macOS keeps in System
Settings (its last alert says so).

Check one thing no one has tested yet: whether the code the downloaded app clones comes out
quarantined (`xattr -l` on `bin/cos`), and whether `cos` still runs from Terminal and from
Claude Code there ([research](research/growing-the-project.md)).

## Each release

1. **The version.** Raise `__version__` in `lib/cos/__init__.py` in a pull request of its own,
   and fold `changes/` into `CHANGELOG.md` under that version. The app's version comes from
   there, and downloaded apps compare it with the newest release's tag, so tag it `v<version>`
   (`v0.8.0`).
2. **The oldest app the code works with.** If the server now needs something only this app
   does, raise `APP_NEEDED` in `lib/cos/core.py` to this version: older downloaded apps then say
   "download Ovenbird.app" instead of breaking.
3. **Build** from a clean checkout of `main`, at `origin/main`, once CI on GitHub has passed
   for that commit (the ruleset doesn't require a merge to be tested on top of what came before
   it, so the squashed commit's own run is the one that counts). From here until it's published,
   nothing merges into main: the release's tag has to be the commit the app was built from.

```sh
COS_RELEASE_PROFILE=~/.claude-os/ClaudeOS-Release.provisionprofile \
COS_NOTARY_PROFILE=claude-os \
app/build.sh --release
```

It refuses to run with uncommitted changes, off `origin/main`, or in a background job, CI or
the evolve sandbox. It then:

- builds the app with nothing machine-specific inside (no paths, a stripped binary);
- checks the profile: Developer ID (any Mac), the release App ID, neutral names;
- scans the whole bundle for anything private before signing, the embedded profile too, apart
  from the team's name, which the signature carries as well;
- signs with Developer ID, the hardened runtime, a secure timestamp and the allowlisted
  entitlements, with no fallback;
- notarizes, staples and zips it to `build/Ovenbird-<version>.zip`.

`--no-notarize` stops before notarizing, for a local check. That zip isn't for publishing.

The app runs on Apple silicon only: `swift build` without `--arch` builds for the Mac it runs
on. Say so in the notes, and in the README's download link.

## Publishing

Only on the owner's yes, each time, in this order. Every `gh release` command asks before it
runs.

1. **Check the zip:** `codesign --verify --deep --strict --verbose=2`, `spctl -a -vv`,
   `xcrun stapler validate` and `lipo -archs` (on `Contents/MacOS/ClaudeOS`) on the app,
   `zipinfo` for `__MACOSX` or extended-attribute entries, and `shasum -a 256` on the zip.
2. **The name on it** (0015, step 2): `codesign -dv --verbose=2` shows the "Authority=Developer
   ID Application: …" line every copy will carry. The owner reads it and says yes. The embedded
   provisioning profile names the same developer account, as its team. On a personal developer
   account, that's the signer's own name.
3. **A draft, pinned to the build's commit:** `gh release create v<version>
   build/Ovenbird-<version>.zip --draft --target <commit> --title "Ovenbird <version>"
   --notes-file <notes>`, with the full 40-character commit the app was built from:
   `/usr/libexec/PlistBuddy -c 'Print :ClaudeOSCommit' build/Ovenbird.app/Contents/Info.plist`.
   The tag must not exist yet (GitHub ignores `--target` for a tag that does). A draft is
   visible only to people with write access to the repository. The notes pass
   `bin/cos guard --text` first, and live in the session's temp folder, not `~/.claude-os` (the
   Guardian won't publish a file from there). They quote only spctl's `accepted` and
   `source=…`, never its `origin=` line or codesign's Authority and TeamIdentifier lines: the
   guard doesn't know the name on the certificate.
4. **The first-launch check** on the draft's zip, the first time and whenever the first launch
   changed (see "Before the first public release" above).
5. **Immutable releases on** (Settings › General › Releases), once: a published release's tag
   and files can't change after that.
6. **Publish:** `gh release edit v<version> --draft=false --latest`. A release that carries the
   app is never a pre-release: the app's update check reads `releases/latest`, which skips
   pre-releases.

If the first-launch check can't happen, publish no app. Delete the draft
(`gh release delete v<version>`), or first take its zip off
(`gh release delete-asset v<version> Ovenbird-<version>.zip`) and publish what's left as a
**pre-release** (`gh release edit v<version> --draft=false --prerelease`, not step 6's
command), so `releases/latest` and the app's update check don't point at a release with no app.
Under immutable releases, the zip then comes as the next version.

### The notes

Plain words, for someone who isn't a developer. Start from this and add what's new:

```markdown
**Ovenbird <version>**: an operating layer for your Mac that Claude lives in.

**What you need:** a Mac with Apple silicon, macOS 14 or later,
[Claude Code](https://claude.com/claude-code) with a Claude plan, and Apple Music for the music
player.

**Install:** unzip, move Ovenbird.app to your Applications folder, and open it. The first launch
shows a checklist: Apple's command line tools (Apple installs them), Claude Code (a link to
Anthropic's page), and Ovenbird itself, downloaded from this repository into
~/Library/Application Support/Ovenbird. Nothing is installed until you click. Two extras, the
skills in Claude Code and a vitals check every 2 hours, start off. Ovenbird never asks for Full
Disk Access, Accessibility or Screen Recording.

**Updates:** while it's open, Ovenbird asks GitHub for a newer Ovenbird.app at most once a day,
and Updates says so when there is one. Download it, replace the old one and open it: Ovenbird
comes along.

**Remove:** Ovenbird menu › Remove Ovenbird… turns off its background jobs, removes its links
and moves its code to the Trash; your journal and memory go only if you say so. Then drag the app
to the Trash. What you allowed it, like Calendar and Reminders, Location, Apple Music and
notifications, stays allowed until you turn it off in System Settings › Privacy & Security, and
System Settings › Notifications; best before you drag the app away.

**What's new:** <from CHANGELOG.md>

Ovenbird is open source under the MIT License, the app included. It isn't made or endorsed by
Anthropic; Claude is Anthropic's trademark.

---

**Verify the download:** commit `<commit>`; SHA-256 of `Ovenbird-<version>.zip`: `<sha256>`;
`spctl -a -vv Ovenbird.app` says `accepted` and `source=Notarized Developer ID`.
```

Credit outside contributors by their GitHub handle, as links rather than @-mentions.
