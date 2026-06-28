# Roadmap

This public roadmap tracks the open-source direction of Surveil. The maintainer may keep a separate private operating plan for personal deployments, credentials, portfolios, and research history.

## Current Focus

- Make setup safe for public forks.
- Keep runtime credentials and personal research data out of the repository.
- Improve source adapters and tests without relying on private data.
- Build a more complete Web workbench for configuration, holdings, source health, and event review.

## Completed Open-Source Prep

- MIT license.
- `.env.example` for non-secret configuration templates.
- Example portfolio and media keyword files.
- Security and compliance documentation.
- Local/server/GitHub Actions deployment documentation.
- Public source catalog covering Serenity, TrendForce, DIGITIMES, Nikkei xTECH, The Elec, official company feeds, Sina Finance, iFinD, and JYGS integration boundaries.
- CI workflow for Python compile checks, shell syntax checks, regression tests, and lightweight secret scanning.
- Manual deploy workflow that pushes code to a user-owned server over SSH.
- Remote helper scripts parameterized by `REMOTE_HOST`, `REMOTE_DIR`, `REMOTE_PROXY_DIR`, and `REMOTE_SERVICE_USER`.
- macOS launchd templates that render the current repository path during installation.

## Near-Term Work

- Add more regression tests for Feishu cards, deduplication, and Web workbench settings.
- Improve systemd service selection so users can enable only the sources they configure.
- Add source health views to the Web workbench.
- Add event review/search in the Web workbench.
- Add a public architecture document for adapters, event storage, LLM gates, and delivery.
- Add Docker/Compose only after the systemd path is stable.

## Contribution Ideas

- Official RSS/API adapters for additional semiconductor and AI infrastructure sources.
- Better article extraction for media pages with noisy navigation text.
- More robust LLM JSON parsing and model-provider compatibility.
- Tests for new data sources and edge cases.
- Web UI improvements for holdings, keywords, source settings, and task health.

## Non-Goals

- Bypassing paywalls, login walls, WAF, or platform access controls.
- Redistributing paid article full text or licensed market data.
- Storing production secrets in the public repository.
- Providing investment advice or automated trading decisions.
