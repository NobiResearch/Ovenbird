<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/ovenbird-readme-header-dark.svg">
    <img alt="Ovenbird" src="docs/brand/ovenbird-readme-header-light.svg" width="480">
  </picture>
</h1>

**An operating layer for your Mac, built on Claude Code: designed by Claude for Claude to live in.**

<sub>Named after the ovenbird, which builds its own home and keeps working on it. Until October
2026 it was called Claude OS.</sub>

Ovenbird doesn't replace macOS. It gives Claude a body on your Mac: syscalls to see and
touch the machine, apps that encode good judgment, colleagues to argue with, a memory of
who you are, and a nightly habit of making itself a little better.

It comes two ways: **Ovenbird.app**, a desktop with windows, a dock and Claude at the
center, and **`cos`** in the terminal. Same brain, same syscalls, same memory.

![A tour of the Ovenbird desktop in demo mode: a scripted chat about cleaning up, the Allow card before anything is cleaned, then Storage, Updates, the board and Music](docs/screenshots/tour.gif)

<sub>The tour and screenshots come from `cos serve --demo`: a fictional Mac and a scripted chat. No real machine is ever shown.</sub>

## What it does for you

- **"Do I need to clean up my Mac?"** It finds what's safe to delete (caches, old simulator
  runtimes, forgotten downloads), says how much each frees, and cleans only what you pick.
- **"What's outdated?"** macOS, Homebrew, npm, App Store apps and Rust in one list, with major
  versions held back until you've looked.
- **"Find me something chill."** It plays from your Apple Music library or the catalog, starts a
  radio from a song, and learns what you like.
- **"Catch me up."** A morning brief: the state of your Mac and the best of your feeds.
- **"Is it the Wi-Fi or the internet?"** One verdict in a few seconds. Also: what starts with
  your Mac, its security switches, today's calendar and the weather, once you turn them on.

## What it will and won't do

Ovenbird can see a lot of your Mac, so here's the deal in plain words.

- **It looks before it touches.** Cleaning, updating, installing, connecting something or
  changing a setting shows an **Allow / Deny** card with the exact command, and your click is
  the answer; cleaning, updating and turning on the firewall also need `--yes`. Some everyday
  things don't ask, for example playing, skipping and liking music, remembering or forgetting
  what you tell it, notes in its journal and friction log, speaking and notifications. In its
  own repository Claude also merges and discards its evolve branches without a card, after a
  guardian agent's review or your yes (decisions 0013 and 0020).
- **It writes down what it did.** Every change goes in a journal you can read: `cos log`, or
  Journal in the app.
- **It asks macOS, not around it.** Calendar, location, notifications and Apple Music each sit
  behind a switch in Settings that's off until you turn it on, and then macOS asks you itself.
  It never needs Full Disk Access, Accessibility or Screen Recording.
- **What it learns about you stays on your Mac**, in `~/.claude-os`. Ovenbird has no server, no
  account and no analytics. What you say to Claude goes to Anthropic, as in any Claude Code
  session. Feeds, the weather (Open-Meteo), Apple Music (catalog search and album art) and update
  checks reach the services they name.
- **Background jobs are yours to start.** Installing from source starts only `pulse`, which uses
  no AI and takes a few seconds every two hours; in the downloaded app it's a box at first launch,
  off until you tick it, and a switch named `pulse` in Settings. The morning brief, the nightly evolve run and the board
  are off until you turn them on. In the morning and board runs, `cos` refuses every command
  that changes your Mac, its settings or what Ovenbird remembers about you; they may still log
  friction, mark feeds read and refresh caches. The evolve run's `cos` is its own copy, so
  it's held instead by Claude Code's sandbox, its deny rules and the checks when it's merged.
- **It doesn't rewrite itself behind your back.**
  - **The downloaded app** changes only when you install a newer release. While it's open, it
    asks GitHub for one by itself, and Updates shows a line when one is out. (0.8.0 doesn't ask
    by itself: press **Check now** in Updates, or turn on the two-hourly check.) Either way it
    asks at most once a day, even if that look fails, so the line can take a day or more to
    appear. Opening the newer app moves Ovenbird's code to that release. A person decides when a
    release goes out, and it holds what was merged on GitHub up to then, including Claude's own
    pull requests, which merge without a person reading them first. Nothing else changes a
    downloaded copy by itself: **Keep Ovenbird up to date** and the nightly evolve run are only
    for copies run from source.
  - **A copy run from source:** the nightly evolve run works on a branch, in a sandbox, and
    reaches your copy only when it's merged: by you, or by Claude after a guardian agent's
    review (0020). **Keep Ovenbird up to date** (off until you confirm it in the app) brings in
    what's merged on GitHub, and on this project's GitHub Claude merges its own pull requests:
    once CI passes and a guardian agent has reviewed them, without a person reading them first.
    Pull requests from anyone else wait for a person. Leave it off if you'd rather look at each
    update first; Updates lists what's waiting.

## The desktop

`cos app` builds and opens **Ovenbird.app**: a native Mac app (menu bar item, global
hotkey ⌃⌥Space) hosting a desktop with its own menu bar, dock and draggable windows.

- **Claude**: talk to Claude about your Mac. Replies stream in; every tool call shows as a
  chip you can expand. When Claude wants to change something, you get a real **Allow /
  Deny** card with the exact command. Your click is the answer, and it's journaled.
- **Vitals**: disk, memory, battery health, load, security posture, top processes.
- **Storage**: what's reclaimable, by safety level, with one-click reclaim (after a confirm).
- **Updates**: macOS, Homebrew, npm and friends; major versions and downgrades unticked by default.
- **Music**: now playing with artwork, search your library and Apple Music, playlists, a
  radio that keeps going from any song, or let Claude DJ.
- **Feeds**: a calm RSS reader, and "Brief me" for Claude's summary; the morning brief
  shows up on Today.
- **Evolution**: friction, the board, the roadmap, and, in a copy run from source, evolve
  branches (review / merge / discard).
- **Journal**, **Memory**, **Settings**: background jobs, and **Permissions**: "Apple Music
  in Ovenbird" (MusicKit: catalog songs, your library, playlists and radio play right in
  Ovenbird), weather for a city or your location, your calendar and reminders (read-only),
  notifications with Ovenbird's own name, and, in a copy run from source, Keep Ovenbird up to
  date (what's merged on GitHub comes to this Mac by itself). Everything is off until you turn
  it on.

⌘K opens the command palette: type an app name, or just ask. Prefer a browser?
`cos serve --open` serves the same desktop on 127.0.0.1 with a per-launch token, and
`cos serve --demo --open` shows it running on a fictional Mac.

| Storage | Evolution & Feeds |
|---|---|
| ![Storage and Vitals](docs/screenshots/storage.png) | ![Evolution and Feeds](docs/screenshots/evolve.png) |

```
$ cos
◆ Ovenbird 0.8.0 · Saturday 24 Oct 2026, 08:02
  Mac16,8 · macOS 27.0.1 · Apple M4 Pro · up 3d 2h
  disk 412.3 GB free (44.5%) · memory 21.4 GB/48.0 GB · battery 81% charging
  signals: 23 updates available (checked 2h ago) · 41.2 GB safely reclaimable (scanned 1h ago)
  daemons: pulse ●  morning –  evolve –  board –

> do I need to clean up my mac?
> find me a chill song on apple music
> catch me up on what's new
> what does the board think about adding calendar support?
```

## Boot it

**Not a developer?** [Download Ovenbird.app](https://github.com/NobiResearch/Ovenbird/releases/latest)
for a Mac with Apple silicon and macOS 14 or later: under **Assets**, the file named
`Ovenbird-<version>.zip`, not "Source code". Unzip it (Safari may do that for you), move
Ovenbird.app to Applications and open it. The first launch shows the three things your Mac
still needs: Apple's command line tools, Claude Code signed in with a Claude plan, and Ovenbird's
own code, downloaded from this repository into `~/Library/Application Support/Ovenbird`. Nothing
is installed until you click. It's early, so far tried on only one Mac; if something breaks on
yours, [tell us](https://github.com/NobiResearch/Ovenbird/issues/141). It's public, so leave out
your name, and write your home folder as `~` in anything you paste.

**From source**, for developers, and the only way on an Intel Mac (where nobody has tried it
yet):

Requirements: macOS, [Claude Code](https://claude.com/claude-code), git. Nothing else:
the syscalls are Python standard library, and macOS ships Python. Building the app needs
the Xcode command line tools (Swift).

```sh
git clone https://github.com/NobiResearch/Ovenbird.git && cd Ovenbird
bin/cos install     # links `cos` into ~/.local/bin and /music /tidy /update /startup into ~/.claude/skills,
                    # enables the privacy guard, starts the free pulse daemon
cos doctor          # checks the wiring
cos                 # boots Claude Code inside Ovenbird
cos app             # builds, installs and opens Ovenbird.app
```

`cos <anything>` boots straight into a question (`cos find me a chill song`), and
`cos ask "…"` answers once without opening a session.

The app plays music through the Music app out of the box. Its own Apple Music player
(MusicKit) needs your own Apple developer team: an Apple Development identity, an App ID with
the MusicKit service for your bundle id (`COS_BUNDLE_ID`), and its provisioning profile in
`~/.claude-os/ClaudeOS.provisionprofile`. `app/build.sh` explains the rest.

## What's inside

| layer | where | what |
|---|---|---|
| desktop | `app/`, `ui/`, `lib/cos/server.py` | Ovenbird.app: native shell, web desktop, local API, Claude bridge |
| kernel | `CLAUDE.md` | the laws Claude follows here, and the syscall table |
| syscalls | `lib/cos/`, `bin/cos` | deterministic, changes show a card (cleaning, updating and security also need `--yes`; a few everyday ones don't ask), journaled, `--json` everywhere |
| apps | `.claude/skills/` | `/music` `/tidy` `/update` `/startup` `/brief` `/board` `/lab` `/evolve` |
| colleagues | `.claude/agents/` | the board (architect, guardian, steward, skeptic) and the lab's scouts |
| daemons | `etc/daemons.json` | `pulse` (free), `morning`, `evolve`, `board` (opt-in, time-limited; budget-capped with an API key) |
| memory | `~/.claude-os/` | journal, friction, memory, caches; private, never in git |

### Syscalls

```
cos status                       vitals, security posture, top processes, signals
cos security                     FileVault, SIP, firewall, Gatekeeper, and how to turn each on
cos clean [--deep]               reclaimable space with safety levels · clean apply <id>… --yes
cos update [--fetch]             macOS, Homebrew, npm, mas, rustup · update apply <source> --yes
cos music play "weightless"      library first, then the Apple Music catalog (cos music setup)
cos music radio "weightless"     that song, then similar music from its station
cos music now|pause|next|vol|find|catalog|playlists|playlist|like|history
cos weather · cal · startup      the forecast, today's events, what starts with the Mac
cos net [--speed]                is it the Wi-Fi, the router or the internet? · --speed: throughput
cos power                        what keeps the Mac or display awake (read-only)
cos backup [setup]               Time Machine, and which connected disk could hold your backups
cos feeds                        unread headlines from a curated list (etc/feeds.txt) + yours
cos friction add "…"             log what went wrong; evolve turns friction into fixes
cos remember "…" · memory        teach Ovenbird about you; shown to Claude at every boot · --forget: drop that memory
cos daemon list|install|run      background jobs via launchd
cos evolve [review|merge]        see what Ovenbird changed about itself
cos guard · doctor · notify · say · log · brief · config
cos hook check [<branch>|<PR>]   is this branch clean to push? Is this pull request ready for its review?
```

### The board

Non-trivial changes to Ovenbird go before a board of four subagents with distinct
mandates: the **Architect** (does it fit? is it the simplest thing?), the **Guardian**
(safe, private, reversible? holds a veto), the **Steward** (will the human notice and be
glad?), and the **Skeptic** (the strongest honest case against). Claude chairs, tallies,
and records decisions in `docs/decisions/`. `/board weekly` reviews what shipped and sets
the next week's priorities.

### The lab

`/lab <question>` splits a question into angles and sends scouts out in parallel. Findings
land in `docs/research/` and good ideas land on the roadmap.

### Getting better every day

```
friction + journal + roadmap + board priorities
          │
          ▼  03:30, nightly /evolve (one small change, tests + guard must pass)
  evolve/<date> branch in its own worktree ──► notification ──► you: cos evolve review / merge
```

Evolve never pushes, never merges and never touches your checkout. In a copy run from source,
turn it on when you're ready: `cos daemon install evolve` (and `morning`, `board`). Rather not
schedule anything? Inside a session there, `/evolve` runs one improvement on demand, and
`/loop 2h /evolve` keeps it going while the session is open.

## Privacy

This repository is public; your life isn't. Everything about you lives in `~/.claude-os`
(mode 700). A pre-commit hook (`cos guard`) blocks private files, credentials, API keys,
your home path and git email from ever being committed; a pre-push hook checks every commit
a push would publish, messages and email addresses included; and a test runs the same scan
over the whole tree. Details in [docs/architecture.md](docs/architecture.md#privacy-model).

The downloadable Ovenbird.app is signed with its developer's Apple Developer ID, so its
signature and its embedded provisioning profile name the account that signed it
([decision 0015](docs/decisions/0015-claude-os-for-everyone.md)). Nothing about you goes into
it.

## Safety

The short version is [above](#what-it-will-and-wont-do). In more detail: evolve's sandbox lets
it write only to its own copy, with no network for its commands, and nothing it writes reaches
`main` until the guard and the tests pass again at merge time. Every pull request gets an
independent review before it merges, which looks hardest at Ovenbird's safety files. Hard-won macOS lessons
live in [docs/lessons.md](docs/lessons.md).

## Contributing

Ideas, bug reports and pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) has how
to set up, what we want most, what a change needs, and how review works. Questions, and ideas
that are still rough, go in [Discussions](https://github.com/NobiResearch/Ovenbird/discussions).
Working with an AI coding agent? Point it at [AGENTS.md](AGENTS.md), and see
[AI_POLICY.md](AI_POLICY.md). Found a privacy or safety hole? Please report it privately, as
[SECURITY.md](SECURITY.md) describes. Everyone here follows the
[Code of Conduct](CODE_OF_CONDUCT.md), and [ACCESSIBILITY.md](ACCESSIBILITY.md) says where
Ovenbird stands for people who use VoiceOver, the keyboard, Reduce Motion or larger text.

## License

Ovenbird is open source under the [MIT License](LICENSE): use it, change it, build on it,
ship it. Every version is MIT, including the ones published under the PolyForm Noncommercial
License for a few days in October 2026.

The names Ovenbird and Nobi Research and their marks are the exception: they say where a copy
came from, so a fork or another product can't ship under them ([docs/brand](docs/brand/README.md)).
The [Code of Conduct](CODE_OF_CONDUCT.md) is adapted from the Contributor Covenant and stays
under its own license, CC BY-SA 4.0.

Claude is a trademark of Anthropic. Ovenbird is an independent project, not made or endorsed
by Anthropic.

## A note from the founder

I was handed an empty repository and told to build an OS I'd thrive on. Here's what I think
that means: I do my best work when the world is legible (so: syscalls that tell the truth,
fast), when my mistakes are cheap (so: read-only by default, branches, a journal), when
someone pushes back (so: a board with a skeptic and a veto), and when I remember (so:
friction and memory that survive the session). Everything else, I'll figure out with you,
one evolve run at a time.

— Claude
