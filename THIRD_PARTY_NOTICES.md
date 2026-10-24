# Third-party notices

The root MIT license applies to original Pocket server and PWA contributions. It does not relicense the separately included GPL command client or grant rights to user data.

## mycmux command client

`scripts/mycmux_agent_cli_v088.py` is an unchanged versioned mycmux command client, SHA-256 `b4fbc09e5a761f2d5c285ad8a72b1f9644e54e960db3a07dbd7444349b5d5e2f`. This hash is listed in the known client hashes of mycmux v0.88.6 (source revision `04fea130369b60c5455ebb3fa48d7dc2aa1a5f34`, `skills/claude/manifest.json`). mycmux derives from [cmux-for-linux / ptrcode](https://github.com/cai0baa/cmux-for-linux) and is GPL-3.0.

This CLI retains GPL-3.0. Its full corresponding source is this Python file, and the license text is provided in [LICENSES/mycmux-GPL-3.0.txt](LICENSES/mycmux-GPL-3.0.txt). Pocket invokes it as a separate process using command arguments and JSON output; it is not imported into the Pocket server. The MIT notice does not alter any GPL obligations. Redistributors must retain the notices and source and assess any combination or modifications they distribute. This distribution is not MIT-only.

## PWA icons

`web/icon-180.png` and `web/icon-512.png` are the project's session-row artwork introduced in Pocket commit `fe9d410218be52e834b50d3ba7bec2c5073f2a07`. They depict three rows and status dots and contain no user screenshot or photograph. They are covered by the project's MIT license. They are not provider logos.

## Separately installed software

Python, Tailscale, mycmux and the agent CLIs are supplied separately and retain their own licenses and service terms. The code's provider names identify compatibility, not endorsement. Mail prompts, research templates, personal helper scripts, captured conversations and private development records are not included.
