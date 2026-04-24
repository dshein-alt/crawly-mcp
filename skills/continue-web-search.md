---
name: Web Search
description: Use crawly-mcp with bounded search and fetch steps so small-context local models can work through web results reliably
invokable: true
---

When the task requires searching and synthesizing across multiple web pages with a small local model:

- If you already have a likely docs URL or landing page, try `page_search(url, query)` before starting another broad `search(...)`.
- Use `search(...)` to collect candidate URLs.
- Keep URL batches small. Default to `1..3` URLs per fetch call.
- Use `fetch(..., content_format="text")` unless HTML structure is explicitly required.
- Treat fetched page content as source material for extraction, not as text to paste wholesale into the next prompt.
- Define a fixed JSON extraction schema before mapping any page.
- Produce one structured record per URL.
- Keep extraction outputs terse and factual.
- Reduce extracted records in small batches instead of one large pass.

Preferred workflow:

1. If the next step is within a known page or docs site, run `page_search(url, query)`.
2. Otherwise run `search(...)`.
3. Select the most relevant URLs only.
4. Run `fetch(urls=[...], content_format="text")`.
5. Extract a fixed JSON record per URL.
6. Merge records in batches.

Notes:

- `page_search(...)` is the bounded path for single-page and site-search style follow-ups.
- Preserve `results_url` from `page_search(...)` when present; it is useful for opensearch/form-backed site search results.

Avoid:

- running another broad `search(...)` when a known page plus `page_search(...)` is enough
- fetching raw HTML for article-style extraction
- fetching many URLs at once with a small model
- summarizing pages in free-form prose before schema extraction
- reducing all page outputs in one prompt
