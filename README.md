# Hermes Discord Clarify

A standalone Hermes Agent plugin that makes Discord multiple-choice clarify prompts readable without changing Hermes core or the gateway callback contract.

## What it changes

When Hermes asks a multiple-choice question in Discord:

- Message content includes the full numbered answer list (`1. ...`, `2. ...`, ...).
- Choice buttons display only their numbers (`1`, `2`, ...), avoiding long labels wrapping on mobile.
- The bundled view callbacks and custom IDs are preserved.
- A numeric click still resolves the original canonical choice text through Hermes' existing resolver path.
- The bundled `Other (type answer)` button remains available for custom answers.
- The existing 24-choice Discord component cap is retained.

Open-ended clarify calls, including empty or invalid choice lists, delegate to the bundled adapter unchanged.
Auth checks, timeout handling, channel and thread resolution, embeds, cron metadata, and unrelated Discord methods remain owned by the bundled adapter.

## Install

Hermes plugins are trusted in-process Python code, so inspect the source before enabling it.
For a reproducible install, pin a full commit SHA:

```bash
hermes plugins install calvinnwq/hermes-discord-clarify \
  --ref <full-commit-sha> \
  --enable
hermes gateway restart
```

For local development, clone the repository into the active Hermes plugin directory instead:

```bash
git clone https://github.com/calvinnwq/hermes-discord-clarify \
  "$HERMES_HOME/plugins/hermes-discord-clarify"
hermes plugins enable hermes-discord-clarify
hermes gateway restart
```

The plugin reuses the Discord credentials already configured for Hermes and adds no runtime dependencies.
For a named Hermes profile, use that profile's `$HERMES_HOME`.

## Verify

List the plugin and run Hermes' isolated plugin validation:

```bash
hermes plugins list
hermes plugins doctor . --ci
```

The plugin should be enabled before restarting the gateway.
A compatible host registers the override under the existing `discord` platform name.
An incompatible host fails safe and leaves the bundled Discord adapter active.

Disable or remove it with:

```bash
hermes plugins disable hermes-discord-clarify
rm -rf "$HERMES_HOME/plugins/hermes-discord-clarify"
hermes gateway restart
```

## Compatibility

The plugin targets Hermes hosts that provide the documented user-plugin `register(ctx)` entry point, `PluginContext.register_platform`, the platform registry, and the bundled Discord adapter symbols `DiscordAdapter`, `ClarifyChoiceView`, and `SendResult`.

It uses runtime feature detection and fails safe on incompatible hosts rather than replacing the bundled adapter with a partially initialized override.
The host must have normal Hermes Discord support installed, including `discord.py`.

The plugin intentionally has no separate bot token, network destination, or credential store.

## Development

Run the focused test suite with the host's development test dependencies:

```bash
python -m pytest -q
```

The tests use fakes for the host adapter and cover:

- numbered message rendering
- numeric button labels
- preserved callback mapping and custom IDs
- the `Other` button
- open-ended delegation
- bounded Discord overflow behavior
- installed-runtime module resolution
- registration metadata preservation
- fail-safe registration on incompatible hosts

## Repository layout

```text
hermes-discord-clarify/
├── __init__.py
├── plugin.yaml
├── README.md
├── LICENSE
├── pytest.ini
└── tests/
    └── test_plugin.py
```

## Trust model

This plugin executes Python inside the Hermes gateway process and can send Discord messages using the bot credentials already configured for Hermes.
Install it only from a source you trust.
It does not collect credentials, add network destinations, or modify Hermes core.

## License

MIT
