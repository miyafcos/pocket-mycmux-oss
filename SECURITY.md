# Security policy

This is a developer source preview, not a hardened public-network service. There is no response-time commitment or claim of a complete security audit.

The server can start or close sessions, inject terminal input, expose permitted local files and change local CLI accounts. Keep it on loopback behind a trusted private-network entry point. Do not disable identity, Host, Origin, CSRF, path validation or file-deny protections to make setup work. Treat pairing and allowed-login configuration as privileged access.

Mail and research integrations are not included: enabling either is rejected before service creation. Disabled mail/research services do not read or resume saved jobs. That does not stop other enabled features from reading the operator's local sessions, skills or configured files. Use a new state folder for evaluation.

Optional relay, Hermes and question-screen helpers require explicit configuration. Only use trusted HTTPS endpoints on your private network; do not put credentials in URLs. A DRY flag or a mock label alone does not establish isolation. Inspect GETs, subprocesses, local writes and cleanup as well as POSTs.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/miyafcos/pocket-mycmux-oss/security/advisories/new) for security details. If the private form is unavailable, ask the maintainer for an approved private contact route without including vulnerability details.

Never post real mail, session logs, uploaded files, tokens, credentials or customer data to a public issue. Use synthetic reproductions. Never send working credentials even in a private report.
