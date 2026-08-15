# Public Repository Rules

## Purpose

This repository publishes stable, reusable Agent Skills for Chinese legal workflows and narrowly scoped local productivity tools maintained by the repository owner. Platform-specific wrappers may improve discovery or installation, but the reusable Skill or independently installable tool is the product. This is a public distribution repository, not a development mirror of private systems.

## Publication boundary

- Never add client data, case materials, workpapers, deliverables, credentials, private configuration, caches, logs, or runtime state.
- Never copy complete private repository history into this repository.
- Import only files approved by an explicit public-release allowlist.
- Use synthetic or fully anonymized fixtures.
- Verify redistribution rights for every imported rule, reference, template, script, and asset.
- Remove machine-specific paths and private organization branding unless publication is expressly authorized.

## Portable Skill and adapter structure

- Put each independently installable package under `plugins/<plugin-name>/`.
- A package may contain `.codex-plugin/plugin.json` as a thin Codex adapter.
- Put the canonical, platform-neutral Skill under `plugins/<plugin-name>/skills/<skill-name>/`.
- Every Skill must contain `SKILL.md` with a stable kebab-case `name` and a clear `description`.
- Keep all business rules, scripts, references and assets in the canonical Skill. Adapters must not fork or duplicate business logic.
- Document how WorkBuddy and other capable Agents can import the same Skill directory, and distinguish design compatibility from runtime-verified compatibility.
- Group Skills only when they serve one coherent workflow and can share a release lifecycle.
- Do not add empty plugin directories or speculative components.

## Standalone tool structure

- Put each independently installable non-Agent utility under `tools/<tool-name>/`.
- Do not fabricate a `SKILL.md` or Codex plugin wrapper for a utility that does not provide Agent behavior.
- Keep source, build and verification scripts, installation documentation, license, notices and any required neutral assets together in the tool directory.
- Commit source and neutral assets; publish generated installer archives as release assets unless a tool-specific rule requires otherwise.
- Document supported platforms, external application dependencies, security permissions, uninstall/recovery behavior and known limitations.

## Release requirements

- Publish only tested, documented, license-cleared capabilities.
- State applicable jurisdiction, source requirements, non-use cases, and human-review gates.
- Run secret, personal-data, local-path, cache, license, and anonymous-test checks before release.
- Use package-specific semantic versions and namespaced tags.
- Keep public documentation concise and understandable to legal professionals.
- Do not commit or push without the maintainer's confirmation.
