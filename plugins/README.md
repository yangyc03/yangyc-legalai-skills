# Agent Skill Packages

Public Agent Skill packages are added here after release review. A package's `skills/<skill-name>/` directory is the canonical portable Skill; `.codex-plugin/plugin.json` is only a thin Codex discovery adapter when present.

Each plugin will use this structure:

```text
plugins/<plugin-name>/
  .codex-plugin/
    plugin.json
  skills/
    <skill-name>/
      SKILL.md
```

## Current candidate

- `local-redaction-assistant` — `1.2.0`
  - Local-only browser-guided DOCX table-aware redaction and manual PDF/image visual copies.
  - Includes session-only JSON dictionary import and explicit high-confidence format-item confirmation.
  - The candidate is distributed under `plugins/local-redaction-assistant/` and remains subject to maintainer review before being treated as a stable release.

- `legal-network-verification` — `2.0.1-beta`
  - Authorized Chinese public-source verification with user-operated authentication handoff, strict zero-result evidence and two-layer workpapers.
  - The same core Skill is intended for Codex, WorkBuddy and other Agents that meet the documented capability requirements.
