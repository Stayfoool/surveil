#!/usr/bin/env python3
"""Regression checks for TrendForce official page extraction."""

from __future__ import annotations

from trendforce_page_monitor import extract_news_items, extract_prnewswire_semi_items, extract_research_items
from trendforce_sources import PageSource


def main() -> int:
    research_source = PageSource(
        "test_research",
        "TrendForce / Research Report / Semiconductors / AI-HBM-Server",
        "https://www.trendforce.com/research/category/Semiconductors/AI%20Server_HBM_Server",
        "research",
        "test",
    )
    research_html = """
    <div class="list-item">
      <a class="title-link" href="https://www.trendforce.com/research/download/RP260615CQ3">
        <strong>AI Inference Era: Server CPU Regains Data Center Centrality</strong>
      </a>
      <h4><i class="fa fa-calendar"></i>2026/06/15</h4>
      <p class="font-size-16 line-height-25 text-ellipsis-2 margin-t-5">
        As AI shifts from training to inference and agentic workloads, Server CPU evolves from auxiliary to core orchestrator.
      </p>
    </div>
    """
    research_items = extract_research_items(research_source, research_html)
    if len(research_items) != 1:
        raise AssertionError(f"expected one research item, got {len(research_items)}")
    if "AI Inference" not in research_items[0]["title"]:
        raise AssertionError("research title extraction failed")
    if "Server CPU evolves" not in research_items[0]["summary"]:
        raise AssertionError("research summary extraction failed")

    multi_card_html = """
    <a href="/research/download/RP260623ZI" class="report-card-small d-flex">
      <div class="card-content">
        <h2 class="card-title">Market Status Update -Jun. 2026</h2>
        <div class="card-meta text-muted">2026/06/23</div>
        <p class="card-desc text-muted">NVIDIA and CSPs drive sustained AI growth through aggressive investment.</p>
      </div>
    </a>
    <a href="/research/download/RP260623JC3" class="report-card-small d-flex">
      <div class="card-content">
        <h2 class="card-title">Probe Card Tech Upgrade &amp; Market Reshaped by AI Packaging-Part 1</h2>
        <div class="card-meta text-muted">2026/06/23</div>
        <p class="card-desc text-muted">AI-driven HPC demand has elevated probe card technology and ATE integration.</p>
      </div>
    </a>
    <a href="/research/download/RP260623YR" class="report-card-small d-flex">
      <div class="card-content">
        <h2 class="card-title">Quarterly Key Component Market Update - 2Q26</h2>
        <div class="card-meta text-muted">2026/06/23</div>
        <p class="card-desc text-muted">The quarterly report covers movements of panel upstream component industry.</p>
      </div>
    </a>
    """
    multi_card_items = extract_research_items(research_source, multi_card_html)
    summaries = {item["title"]: item["summary"] for item in multi_card_items}
    if "probe card technology" not in summaries.get("Probe Card Tech Upgrade & Market Reshaped by AI Packaging-Part 1", ""):
        raise AssertionError("research cards should not reuse the previous card summary")
    if "panel upstream" not in summaries.get("Quarterly Key Component Market Update - 2Q26", ""):
        raise AssertionError("research cards should not shift summaries across cards")

    news_source = PageSource(
        "test_news",
        "TrendForce / News / Semiconductors",
        "https://www.trendforce.com/news/category/semiconductors",
        "news",
        "test",
    )
    news_html = """
    <h2 class="text-ellipsis-2">
      <a class="title-link" href="https://www.trendforce.com/news/2026/06/17/news-tsmc-amkor-forge-10-year-arizona-advanced-packaging-partnership/">
        <strong>[News] TSMC, Amkor Sign 10-Year Arizona Advanced Packaging Pact</strong>
      </a>
    </h2>
    <p>Advanced packaging capacity expands for AI chips and the U.S. semiconductor supply chain.</p>
    """
    news_items = extract_news_items(news_source, news_html)
    if len(news_items) != 1:
        raise AssertionError(f"expected one news item, got {len(news_items)}")
    if "2026-06-17" not in news_items[0]["published_at"]:
        raise AssertionError("news date extraction failed")

    semi_source = PageSource(
        "semi_prnewswire_semiconductors",
        "SEMI / PR Newswire / Semiconductors",
        "https://www.prnewswire.com/news-releases/business-technology-latest-news/semiconductors-list/",
        "prnewswire_semi",
        "test",
    )
    semi_html = """
    <div role="group" aria-label="News Release" class="card col-view">
      <a class="newsreleaseconsolidatelink display-outline w-100"
         href="/news-releases/semi-projects-300mm-memory-equipment-investment-to-surpass-50-billion-in-2026-302811354.html">
        <div class="col-sm-8 col-lg-9 pull-left card">
          <h3><small>08:00 ET</small><span>SEMI Projects 300mm Memory Equipment Investment to Surpass $50 Billion in 2026</span></h3>
          <span><p class="remove-outline">SEMI today projected strong semiconductor equipment demand from AI and memory fabs.</p></span>
        </div>
      </a>
    </div>
    <div role="group" aria-label="News Release" class="card col-view">
      <a class="newsreleaseconsolidatelink display-outline w-100"
         href="/news-releases/random-sensor-company-launches-product-302811355.html">
        <div><h3><span>Random Sensor Company Launches Product</span></h3><p>Not a SEMI release.</p></div>
      </a>
    </div>
    """
    semi_items = extract_prnewswire_semi_items(semi_source, semi_html)
    if len(semi_items) != 1:
        raise AssertionError(f"expected one SEMI item, got {len(semi_items)}")
    if "300mm Memory Equipment" not in semi_items[0]["title"]:
        raise AssertionError("SEMI PR Newswire title extraction failed")
    if "PR Newswire" not in semi_items[0]["body_source"]:
        raise AssertionError("SEMI body source should identify PR Newswire")

    print("trendforce page extraction checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
