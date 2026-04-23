---
name: Web Search
description: Use crawly-mcp with bounded search and fetch steps so small-context local models can work through web results reliably
invokable: true
---

When the task requires searching and synthesizing across multiple web pages with a small local model:

- Use `search(...)` to collect candidate URLs.
- Keep URL batches small. Default to `1..3` URLs per fetch call.
- Use `fetch(..., content_format="text")` unless HTML structure is explicitly required.
- Treat fetched page content as source material for extraction, not as text to paste wholesale into the next prompt.
- Define a fixed JSON extraction schema before mapping any page.
- Produce one structured record per URL.
- Keep extraction outputs terse and factual.
- Reduce extracted records in small batches instead of one large pass.

Preferred workflow:

1. Run `search(...)`.
2. Select the most relevant URLs only.
3. Run `fetch(urls=[...], content_format="text")`.
4. Extract a fixed JSON record per URL.
5. Merge records in batches.

Avoid:

- fetching raw HTML for article-style extraction
- fetching many URLs at once with a small model
- summarizing pages in free-form prose before schema extraction
- reducing all page outputs in one prompt
