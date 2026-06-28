# Security

Surveil handles high-value credentials. Treat the repository as public by default.

## Never Commit

- `.env` or `.env.*`
- API keys, refresh tokens, bearer tokens, cookies, webhooks, or secrets
- Real portfolio files
- SQLite databases
- Logs and generated reports
- Proxy subscriptions or Clash/Mihomo YAML files
- Downloaded binaries

## Recommended Secret Handling

- Use `.env.example` only as a template.
- Keep production secrets in the server `.env`.
- Use the Web workbench settings page or SSH helper scripts to update secrets.
- Sensitive values are write-only in the Web workbench: blank means keep current, nonblank means overwrite.
- Rotate tokens immediately if they were pasted into chat, logs, issues, screenshots, or commits.

## Before Publishing

Run a scan like:

```bash
python scripts/scan_secrets.py
```

Also inspect:

```bash
find . -maxdepth 3 -type f | sort
```

If secrets were committed in git history, do not push that history. Rebuild a clean repository or remove the secrets with a history rewriting tool before publishing.

## GitHub Actions

Use GitHub Secrets only for deployment credentials. Prefer keeping business/runtime secrets on the target server, not in GitHub.

Suggested GitHub Secrets for deployment:

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `DEPLOY_DIR`
- `DEPLOY_SERVICE_USER`

Avoid storing iFinD/X/Feishu/JYGS credentials in GitHub unless you fully accept that risk.
