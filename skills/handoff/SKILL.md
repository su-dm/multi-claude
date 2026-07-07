---
name: handoff
description: Write or update HANDOFF.md — a session-handoff / progress file capturing what was done, what's next, and what a fresh session must know to continue this project. Use when ending a session, before context gets tight, or when the user asks to "save progress", "write a handoff", or "update the journal".
---

# Session handoff / progress file

Write (or update) `HANDOFF.md` at the project root so a fresh session — you
tomorrow, another agent, or a human — can continue without re-deriving
context. This file is for a reader with ZERO memory of this session.

If the project already tracks progress under a different name (JOURNAL.md,
NOTES.md, docs/STATE.md), update THAT file in its existing style instead of
creating a competing one.

## Structure

```markdown
# Handoff — <project name>

Updated: <YYYY-MM-DD> · <one-line project purpose>

## Now / Next
<The single most important thing to do next, stated so it can be started
immediately: file, function, command, or decision. Then 2-5 more, ordered.>

## State of play
<What is DONE and verified (say how it was verified — tests, manual check).
What is IN PROGRESS and exactly where it stopped (file:line if applicable).
What is BLOCKED and on what.>

## Open decisions
<Questions the next session must answer or get answered by the user, with
the options considered and any leanings + why.>

## Landmines & lessons
<Non-obvious facts discovered the hard way: env quirks, flaky tests, APIs
that lie, ordering constraints, things that LOOK wrong but are correct.
This is the highest-value section — never skip it.>

## Map
<Pointers, not copies: key files with one-line roles, relevant branches/PRs/
issues/URLs, how to run and test the thing.>
```

## Rules

- **Verified vs assumed:** every "done" claim must trace to something you
  actually observed this session (a passing test, an output). Mark anything
  unverified as `(unverified)`.
- **Update, don't append:** rewrite sections to reflect current reality;
  stale "next steps" that are now done must go. Keep dated history only if
  the existing file's convention has it.
- **Don't duplicate the repo:** no code listings, no git history retelling —
  point at them.
- **Convert relative time:** "yesterday" → an absolute date.
- Target under 120 lines. If it's growing past that, the Landmines and Map
  sections stay; trim narrative first.
