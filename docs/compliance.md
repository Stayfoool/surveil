# Compliance

Surveil is designed for personal research workflows using official or authorized sources.

## Source Policy

Prefer:

- Official APIs
- Official RSS/Atom/RDF feeds
- Public pages that allow normal access
- User-authorized APIs or exports

Avoid:

- Bypassing paywalls
- Bypassing login walls
- Circumventing WAF, bot challenges, or rate limits
- Copying full paid articles into logs, reports, issues, or fixtures
- Using unknown mirrors for official content

## Paid or Logged-in Sources

If a data source requires a subscription, login, cookie, or token:

- Use only access methods permitted by that source.
- Store credentials only in `.env` or another private secret store.
- Do not publish the retrieved paid/full text content in the repository.
- Keep automated access low-frequency and respectful of terms and rate limits.

## X / Social Media

Use official APIs where available. Do not scrape private or paid content unless the platform explicitly provides an authorized API path for your account and use case.

## Market Data

Some data providers restrict redistribution. The project should store and display only what your license permits. When in doubt, keep raw data private and publish only code, schemas, and examples.

## Investment Disclaimer

Surveil generates research candidates and summaries. It is not investment advice, not a recommendation system, and not a substitute for independent judgment.
