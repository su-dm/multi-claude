---
name: condense-to-skill
description: Condense a technique worked out in this session into a reusable skill file, saved under .claude/skills/ (project) or ~/.claude/skills/ (global). Use when the user wants to capture "how we just did X" so future sessions can repeat it without rediscovering it.
---

# Condense session work into a skill

Turn something non-obvious that was figured out in THIS session into a small,
reusable skill. The bar: if the next session could not reproduce the result
from the repo and docs alone, it belongs in a skill; if it could, do not
create one.

## Steps

1. **Identify the skill-worthy core.** Scan the session for the technique
   that took real discovery: the working sequence of commands, the API quirk
   and its workaround, the config incantation, the debugging recipe. One
   skill = one capability. If the session contains two unrelated techniques,
   ask which one to capture (or make two files).
2. **Ask yourself what was NON-OBVIOUS.** Strip everything a competent agent
   would do anyway. Keep the trap doors: exact flags that matter, ordering
   constraints, error messages seen and what they actually meant, values
   that must not be guessed.
3. **Choose scope.** Project-specific → `<repo>/.claude/skills/<name>/SKILL.md`.
   Broadly reusable → `~/.claude/skills/<name>/SKILL.md`. If unclear, prefer
   project scope.
4. **Write the file** using the template below. The `description` line is
   what future sessions use to decide relevance — write it as "Use when …"
   with concrete trigger words.
5. **Verify**: re-read the skill pretending you know nothing about this
   session. Every command must be copy-pasteable; every claim must have been
   actually verified in this session (mark anything unverified as such).
   State in your reply where the file was saved and what would trigger it.

## Template

```markdown
---
name: <kebab-case-name>
description: <one sentence: what it does + "Use when <trigger>">
---

# <Title>

<2-3 sentence overview: what this achieves and when to reach for it.>

## Steps

<Numbered, copy-pasteable. Include exact commands/flags/paths.>

## Pitfalls

<The errors you WILL hit if you deviate, and what they look like.
This section carries most of the value — it is the discovered knowledge.>

## Verified

<Date, environment, and how the result was confirmed.>
```

## Rules

- Never invent steps that were not exercised in the session.
- Shorter is better: a skill the model won't finish reading is a skill that
  doesn't exist. Target under 80 lines.
- If an existing skill already covers this, UPDATE it instead of creating a
  near-duplicate (check the target skills directory first).
