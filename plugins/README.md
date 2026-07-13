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

- `local-redaction-assistant` — `1.1.1-beta`
  - Local-only browser-guided redaction for explicitly selected DOCX copies and manual PDF/image visual copies.
  - The candidate is distributed under `plugins/local-redaction-assistant/` and remains subject to maintainer review before being treated as a stable release.
