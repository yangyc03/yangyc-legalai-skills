# Public Repository Rules

## Purpose

This repository publishes stable, reusable Codex plugins and Skills for Chinese legal workflows. It is a public distribution repository, not a development mirror of private systems.

## Publication boundary

- Never add client data, case materials, workpapers, deliverables, credentials, private configuration, caches, logs, or runtime state.
- Never copy complete private repository history into this repository.
- Import only files approved by an explicit public-release allowlist.
- Use synthetic or fully anonymized fixtures.
- Verify redistribution rights for every imported rule, reference, template, script, and asset.
- Remove machine-specific paths and private organization branding unless publication is expressly authorized.

## Plugin and Skill structure

- Put each independently installable package under `plugins/<plugin-name>/`.
- Every plugin must contain `.codex-plugin/plugin.json`.
- Put bundled Skills under `plugins/<plugin-name>/skills/<skill-name>/`.
- Every Skill must contain `SKILL.md` with a stable kebab-case `name` and a clear `description`.
- Group Skills only when they serve one coherent workflow and can share a release lifecycle.
- Do not add empty plugin directories or speculative components.

## Release requirements

- Publish only tested, documented, license-cleared capabilities.
- State applicable jurisdiction, source requirements, non-use cases, and human-review gates.
- Run secret, personal-data, local-path, cache, license, and anonymous-test checks before release.
- Use plugin-specific semantic versions and tags.
- Keep public documentation concise and understandable to legal professionals.
- Do not commit or push without the maintainer's confirmation.
