# Repository Skills

> **Upstream source:** [f-steff/KUAIQU-NICE-PSU](https://github.com/f-steff/KUAIQU-NICE-PSU)
> (`skills/README.md`). This file is copied into other repositories together with
> the `scripts/` and `skills/` files it documents. The GitHub repository is
> authoritative: fix issues there and re-copy, so the mirrors do not drift apart.

This directory is the hand-edited, portable source of truth for agent skills.
Each skill lives in `skills/<name>/` with a `SKILL.md` (Codex/Claude/opencode
compatible YAML frontmatter plus Markdown instructions). To use a skill, ask an
agent to install it into the directory its host reads - for example `.agents/`
for Codex or `.claude/` for Claude Code - recreating host-specific extras there
as needed.

## Layout

```
skills/<name>/SKILL.md   # skill instructions (name + description frontmatter)
```

## Frontmatter

```yaml
---
name: skill-name
description: Use when ...
---
```

- `name` must match the directory name and be lowercase hyphen-separated.
- `description` must cover both what the skill does and when to trigger it.
- Host-specific extras (for example Codex's `agents/openai.yaml`) are not stored
  here; the installing agent recreates them in the destination if wanted.

## Discovery

- opencode reads `skills/` in place via `opencode.jsonc` (`skills.paths`), so no
  install step is needed for opencode.
- Codex scans `.agents/skills` and Claude Code scans `.claude/skills`; copy the
  skill folder into the location each host reads. All hosts use the same
  `SKILL.md` frontmatter shape, so no generated mirrors are required.
