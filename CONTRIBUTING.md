# Contributing

This is a developer source preview. Keep changes focused and provide a synthetic reproduction. Use the private route in SECURITY.md for vulnerabilities.

Do not copy production state, credentials, captured conversations or customer data. Before importing `server.py`, isolate state paths, the home directory, subprocesses and network requests: import initializes machine configuration and can query Tailscale.

The bundled public-default checks extract functions without importing the server:

```sh
python -m pytest --noconftest -p no:cacheprovider tests/ -q
node tests/test_public_hub.mjs
```

These checks do not replace authentication, CSRF, path traversal, concurrency or real-device tests. Do not disable protections to make a new environment work. Mail/research integrations are deliberately unavailable in this distribution and must not be enabled without a separately reviewed implementation and tests.

The root MIT license covers original contributions. Keep the GPL-3.0 license and source of `scripts/mycmux_agent_cli_v088.py`; do not label the complete distribution MIT-only. Document the source and license of every added image, dataset and borrowed component.
