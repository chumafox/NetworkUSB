# Security Policy

## Reporting Vulnerabilities

If you discover a potential security vulnerability in NetworkUSB, please do NOT create a public issue.
Instead, report it directly to the maintainers at `security@chumafox.org` or open a private security advisory on GitHub.

## Security Controls & Best Practices

NetworkUSB implements the following core security controls:
- Mandatory TLS 1.3 encryption over TCP endpoints.
- Pre-auth SHA-256 fingerprint verification (`--expected-fingerprint`).
- Private socket modes (`0700` default) for local usbmuxd endpoints.
- Secret tokens passed via environment or permission-checked `--token-file` (`0600`).
- Constant-time token verification (`secrets.compare_digest`).
