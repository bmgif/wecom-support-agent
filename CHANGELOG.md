# Changelog

## Unreleased

- Add a FastAPI webhook for a single WeChat Customer Service test account, with signed callback verification and a local SQLite ledger.
- Pull and deduplicate test messages, queue the exact `测试转人工` command for human service, and record the human pickup event from WeCom.
- Document Linux and Caddy deployment for a fixed HTTPS callback, with a separate Windows guide for local development.
