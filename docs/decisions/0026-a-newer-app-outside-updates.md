# 0026 — A newer app says so outside Updates

- **Status:** accepted
- **Date:** 2026-11-01
- **Decided by:** the board, on the question pull request 179 left open
- **Amends:** 0015, step 6 ("a quiet line in Updates")

## Context

#179 made a downloaded app look for a newer release by itself, once a day, from the status poll.
The answer goes into the cached `updates` results. Someone with no results has nothing to add
to, so the look ran and nothing showed until they opened Updates. `pulse`, which would otherwise
fill those results, is off until ticked at first launch.

It matters more for a download than for anything else on the Mac. A download's code follows its
app (0015), so it gets no code at all, safety fixes included, until the person replaces the app.
That same fact makes this a choice that can't wait: a copy can only show a line its own release
carries. 0.8.0 copies can never get this, and each release shipped without it leaves another group
who won't hear.

## Votes

| Seat | Vote | Top reason |
|---|---|---|
| Architect | amend, always named | `status.signals` is where cached state already becomes a line, and it reaches every surface with no plumbing. A line shown only while there are no results loses the app's name the moment there are. |
| Guardian | amend, only while there are no results | Leaving it means an agent with broad access runs old safety code and nothing says so. Text from GitHub is safe if only the version goes in. Being named in every session after the person has seen the card is the cost of the other shape, though the risk is the same. |
| Steward | amend, always named | A stranger can't be told later. A red badge wouldn't be quiet. |
| Skeptic | amend, always named | Leaving it is the one choice that can't be repaired. One click on the welcome card's Updates link would end a line shown only while there are no results. |

No seat voted to leave it. The Guardian's preference isn't a veto: its vote was amend, and it said
both shapes carry the same risk.

## Decision

One `note` in `status.signals`: `Ovenbird.app 0.9.0 is out: see Updates`.

- **It reads wherever signals do:** Today, the menu bar's dot and menu, Vitals, the boot banner and
  what the morning brief reads.
- **It's always named.** One visit to Updates, or one `cos update` from the "What's outdated?" chip,
  fills the results, and `pulse` does too. After that a count can't say that one of them is the
  update that brings Ovenbird's own code. So the app can be said twice, by name and in the count.
  That's honest, and it's one condition fewer.
- **It stays until the newer app is opened.** No dismiss: the line is true, it ends by itself, and
  a dismiss would be state that silences the only channel a safety fix has. It doesn't depend on
  the welcome card.
- **It never makes the look.** `updates.known_newer_app` reads the last one and that's all: no
  network, no `LOOKING`, nothing written, because `signals` runs unguarded on every poll, in the
  boot banner and in `pulse`. A copy with no release app reads no look at all. Anything unreadable
  is no line and no exception.
- **Updates and the signal can't disagree.** Both ask `updates.newer_app`.
- **The words are fixed.** Only the version comes from GitHub, and `core.version_tuple` now takes
  ASCII digits and bounded lengths only, since a version reaches Claude's boot banner. No URL and
  no release text go in a signal.
- **It's always a note,** never chosen from anything in the release. `pulse` turns a `warn` into a
  notification, so a louder level would break "no notification", and a line that can't tell a
  safety release from any other shouldn't claim to. Anything louder needs a new board decision.
- **One problem, one line.** An app too old for its code already has its warning, so it gets no
  note as well.
- **`/update` knows there's no command for it:** the link and three steps, never git. The banner
  now puts this line in the chat, so "update it" is a likely reply.

0015's limits stay: no dialog, no notification, no self-update, no new call from a copy run from
source, and a failed look stays quiet.

## What this can't do

- **0.8.0 copies never get this.** The first release that carries it can't announce itself, only
  the one after can. So it has to land before the next release is cut.
- **"See Updates" is a pointer, not a link.** With no results, Updates runs the full check first
  and the card comes at the end of it. It took 3 seconds on one Mac, and `softwareupdate` can hold
  it for up to 120.
- **The line is one note among others.** It doesn't say a release matters more than another,
  because it doesn't know.

## Not now

One seat each asked for these, and the seats that took them up accepted going without for now:

- a click on Today's line that opens Updates (signals stay level-and-message pairs);
- leaving the app out of the count;
- showing the card at once from the cached look, above the full check;
- "1 update" for "1 updates".

Turned down: a badge on the Updates tile (a wordless red number that can't be dismissed), a line on
Today only (a new status field, and neither Claude nor the menu bar hears it), and having `fold_app`
write results with only the app in them (Updates would print "Up to date." for macOS without
having looked, and `pulse` would skip its check for 20 hours).
