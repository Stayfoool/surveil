# Contributing

Thanks for considering a contribution.

Good contribution areas:

- New official RSS/API adapters
- Parser fixes
- Web workbench improvements
- LLM prompt and JSON parsing robustness
- Tests for source parsing, deduplication, cards, and failure handling
- Documentation and deployment examples

## Ground Rules

- Do not include secrets, cookies, tokens, private reports, real portfolios, logs, or databases.
- Do not add code that bypasses paywalls, login walls, WAF, or source access controls.
- Prefer official APIs and official feeds.
- Keep changes small and testable.
- Add or update tests when changing parsing, deduplication, LLM JSON handling, or delivery behavior.

## Local Checks

```bash
python -m py_compile scripts/*.py
python scripts/test_analysis.py
python scripts/test_llm_analysis.py
python scripts/test_trendforce_page_monitor.py
python scripts/test_link_enrichment.py
python scripts/test_sina_stock_news.py
```

Some tests may require optional credentials or network access. Keep credential-dependent checks optional.
