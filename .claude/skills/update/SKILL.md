---
name: update
description: Keep macOS and the software installed on this Mac up to date: macOS, Homebrew, global npm packages, App Store apps, Rust toolchains. Use when the human asks about Mac updates, outdated Homebrew or global tools, App Store apps, or security patches. Not for a project's own dependencies, lockfile or package.json.
---

> Works from any folder on this Mac ([0009](../../../docs/decisions/0009-claude-os-everywhere.md)). Run syscalls as `cos …`; in the Ovenbird folder, `bin/cos …` is the same. Whatever you `cos remember` or `cos friction add` is about the human or this Mac, never the project or client you're working in.

# Update

## Steps

1. **Check.** `cos update --fetch --json` (refreshes Homebrew metadata; ~10–60s).
2. **Triage** into three groups and keep it short:
   - **Do now**: macOS security updates, and anything that is a security fix you know of.
   - **Routine**: patch/minor bumps of Homebrew formulae, casks, npm globals. Batch them.
   - **Look first**: major-version bumps (▲), downgrades where "latest" is lower than what's
     installed (▼, usually a tap or formula rollback: say so, don't apply by default), new
     version schemes (≠, a build number became a version), pinned packages, and things that
     tend to break toolchains (python, node, openssl, postgres, llvm). Say why in a few words.
3. **Ask** what to apply. Offer a sensible default ("routine now, majors later?").
4. **Apply** what's approved:
   - `cos update apply brew --yes` (all) or `cos update apply brew <names…> --yes`
   - `cos update apply npm [names…] --yes`, `cos update apply mas --yes`
   - macOS: `cos update apply macos` opens System Settings; installing needs the
     human's password and a restart. Never try `sudo softwareupdate`.
5. **Verify** with `cos update` and report what changed and anything that failed.

## Rules

- Long upgrades can take many minutes; tell the human before starting a big batch.
- A newer Ovenbird.app has no `cos update apply`: give the link `cos update` shows and the three
  steps (download it, replace the one in Applications, open it). Never update a downloaded
  Ovenbird with git: its code comes with the app.
- If an upgrade fails, show the last lines of output and log friction if it looks like
  something Ovenbird should handle better.
