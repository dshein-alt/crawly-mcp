from __future__ import annotations

import pytest

from crawly_mcp.models import PageSearchResult
from crawly_mcp.page_search import TextHit, TextTier


def test_text_tier_detect_always_returns_hit() -> None:
    tier = TextTier()
    hit = tier.detect(
        "<html><title>T</title><body>hello</body></html>",
        "https://example.com/",
    )
    assert isinstance(hit, TextHit)
    assert hit.title == "T"


@pytest.mark.asyncio
async def test_text_tier_execute_returns_snippets() -> None:
    html = (
        "<html><title>Docs</title><body>"
        "<p>fetch returns structured content</p>"
        "</body></html>"
    )
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert len(results) == 1
    assert isinstance(results[0], PageSearchResult)
    assert "structured" in results[0].snippet.lower()
    assert results[0].url is None
    assert results[0].title == "Docs"


@pytest.mark.asyncio
async def test_text_tier_execute_empty_on_no_match() -> None:
    html = "<html><title>T</title><body>nothing relevant here</body></html>"
    tier = TextTier()
    hit = tier.detect(html, "https://example.com/")
    results = await tier.execute(hit, "structured")
    assert results == []
