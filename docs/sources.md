# Source Catalog

Surveil ships with a reusable source catalog focused on semiconductors, AI infrastructure, data centers, memory, advanced packaging, optical interconnects, and related supply chains.

The catalog is public configuration and code. Credentials, cookies, paid-content access, personal usernames, real portfolios, and server settings stay private.

## Why These Sources

| Source Group | Influence / Signal Value |
| --- | --- |
| Serenity on X | Useful for market-facing interpretation of AI infrastructure, photonics, memory, CPO, and global semiconductor equity narratives. It is not an official data source; treat it as a high-signal opinion stream that still needs verification. |
| TrendForce | A widely cited research provider for memory, panels, foundry, components, AI servers, MLCC, and pricing/supply-demand trends. Its public headlines and summaries often flag important supply-chain direction before broader market discussion. |
| DIGITIMES | Taiwan supply-chain coverage is especially relevant to TSMC, IC design, advanced packaging, servers, ODMs, PCBs, components, and AI hardware manufacturing. |
| Nikkei xTECH | Japan is important in semiconductor equipment, materials, components, industrial automation, and automotive electronics. Nikkei xTECH helps surface Japan-side technology and supply-chain changes. |
| The Elec | Korea is central to memory, HBM, OLED/display, batteries, equipment, and materials. The Elec can surface Samsung/SK hynix/LG-adjacent supply-chain signals. |
| Official company feeds | First-party announcements from OpenAI, NVIDIA, Samsung Semiconductor, SK hynix, and Micron are primary sources for architecture, product, capex, platform, and supply-chain changes. |
| Sina Finance / iFinD / JYGS | China-market channels for holdings-related news, official company notices, announcements, and A-share event/action monitoring. |

## X Accounts

| Source | Default Account | Method | Notes |
| --- | --- | --- | --- |
| Serenity | `aleabitoreddit` | X API filtered stream or polling | Configure privately with `X_USERNAME=aleabitoreddit` and your own X API credentials. Public posts only unless X provides an authorized API path for your account. |

Surveil does not commit X tokens. The repository only contains the monitor logic.

## Official Company Feeds

These feeds are included in `scripts/trendforce_sources.py` through `DEFAULT_RSS_FEEDS`.

| Source Key | Source | URL | Method |
| --- | --- | --- | --- |
| `openai_news` | OpenAI News | `https://openai.com/news/rss.xml` | RSS |
| `nvidia_blog` | NVIDIA Blog | `https://blogs.nvidia.com/feed/` | RSS |
| `nvidia_developer_blog` | NVIDIA Developer Blog | `https://developer.nvidia.com/blog/feed/` | Atom/RSS |
| `samsung_semiconductor_news` | Samsung Semiconductor News | `https://news.samsungsemiconductor.com/global/feed/` | RSS |
| `samsung_global_semiconductor` | Samsung Newsroom Semiconductors | `https://news.samsung.com/global/category/products/semiconductor/feed` | RSS |
| `skhynix_newsroom` | SK hynix Newsroom | `https://news.skhynix.com/feed/` | RSS |
| `micron_news_releases` | Micron News Releases | `https://investors.micron.com/rss/news-releases.xml` | RSS |

Official company news goes through an LLM importance gate. High-impact semiconductor/AI infrastructure items can be pushed immediately; lower-signal items can be collected into a daily digest.

## TrendForce

Surveil includes TrendForce RSS categories and public list-page monitors.

RSS categories:

| Source Key | URL |
| --- | --- |
| `trendforce_semiconductors` | `https://www.trendforce.com/feed/Semiconductors.html` |
| `trendforce_emerging` | `https://www.trendforce.com/feed/Emerging_technology.html` |
| `trendforce_consumer` | `https://www.trendforce.com/feed/Consumer_electronics.html` |
| `trendforce_energy` | `https://www.trendforce.com/feed/Energy.html` |
| `trendforce_display` | `https://www.trendforce.com/feed/Display.html` |
| `trendforce_led` | `https://www.trendforce.com/feed/LED.html` |
| `trendforce_communication` | `https://www.trendforce.com/feed/Communication.html` |

Public page monitors include:

- Research Report latest
- DRAM
- NAND Flash
- MLCC
- Wafer Foundries
- Compound Semiconductor
- AI Server / HBM Server
- Cloud and Edge Computing
- Artificial Intelligence
- Display Supply Chain
- Upstream Components
- IR LED / VCSEL / LiDAR Laser
- Lithium Battery and Energy Storage
- Selected Topics: Semiconductors, Telecommunications, Computer System, Green Energy and Storage, Display Panel and LED
- Press Centre In-Depth Analyses

Research Report and Selected Topics pages may contain member or paid content. Surveil only reads public list-page titles/summaries and does not bypass access controls.

## Industry Media

These sources are defined in `scripts/media_sources.py`.

| Source Key | Source | URL | Method |
| --- | --- | --- | --- |
| `digitimes_tw_semiconductors_components` | DIGITIMES Taiwan / Semiconductors and Components | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_10_40.xml` | RSS |
| `digitimes_tw_ic_design` | DIGITIMES Taiwan / IC Design | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_16.xml` | RSS |
| `digitimes_tw_ic_manufacturing` | DIGITIMES Taiwan / IC Manufacturing | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_17.xml` | RSS |
| `digitimes_tw_ai_focus` | DIGITIMES Taiwan / AI Focus | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_25.xml` | RSS |
| `digitimes_tw_server` | DIGITIMES Taiwan / Server | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_30_26.xml` | RSS |
| `digitimes_en_daily` | DIGITIMES English Daily | `https://www.digitimes.com/rss/daily.xml` | RSS |
| `nikkei_xtech_all` | Nikkei xTECH | `https://xtech.nikkei.com/rss/index.rdf` | RDF |
| `thelec_kr_semiconductor` | The Elec Korea / Semiconductor | `https://www.thelec.kr/rss/S1N2.xml` | RSS |
| `thelec_kr_all` | The Elec Korea / All Articles | `https://www.thelec.kr/rss/allArticle.xml` | RSS |

These feeds are filtered by configurable media keywords before LLM gating. The default keywords cover AI, semiconductors, HBM, MLCC, advanced packaging, PCB, glass substrates, liquid cooling, optical interconnects, diamond cooling, and related infrastructure.

## Sina Finance and iFinD

| Source | Method | Credentials |
| --- | --- | --- |
| Sina Finance news | OpenAPI, MCP backup, or legacy public pages | `SINA_ZY_API_KEY` if using OpenAPI |
| iFinD notices | iFinD REST/API | `IFIND_REFRESH_TOKEN` or access token |

iFinD is the preferred source for company notices/announcements. Sina news filters out announcement-like reposts where possible so iFinD remains the authoritative notice path.

## JYGS

JYGS action analysis is supported as a low-frequency monitor. It requires user-authorized private configuration:

- `JYGS_COOKIE` or `JYGS_SESSION`
- `JYGS_SIGN_SECRET`

Do not commit these values. If the source changes login, signing, or access rules, use only authorized access paths.

## Customization

Users can customize:

- Holdings and watchlist: `config/portfolio.json` or the Web workbench
- Media keywords: `config/media_keywords.json` or the Web workbench
- LLM provider: `.env` `LLM_*`
- Enabled services: systemd units/timers or local commands

The public examples are intentionally generic. Runtime choices belong in private `.env` and local config files.
