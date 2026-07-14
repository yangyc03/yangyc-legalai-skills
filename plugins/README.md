# Plugins

Public Codex plugins are added here after release review.

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
