# Security policy

DevDock is an early local development tool. Do not use it to manage production infrastructure or store credentials.

## Reporting a concern

Do not open a public issue containing credentials, personal information, private project details, or a working exploit. Contact the project owner through the private reporting option on the repository host, or ask for a private reporting channel if one is not available. Include only the information needed to reproduce the issue using a disposable environment.

## Security expectations

- Treat project profiles and Compose files as untrusted input.
- Never commit secrets or real user/customer data, including in test fixtures or Git history.
- Keep automated tests on synthetic, disposable projects.
- Report command execution, path handling, secret disclosure, unsafe cleanup, and cross-project resource access issues privately.

