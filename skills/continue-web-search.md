---
name: Web Search
description: Use crawly-mcp with bounded search and fetch steps so small-context local models can work through web results reliably
invokable: true
---

When the task requires searching and synthesizing across multiple web pages with a small local model:

- **Read `status` and `next_action` before concluding anything.** `no_results` means a provider searched and found nothing, so reword the query. `blocked` / `unavailable` / `timeout` / `navigation_failed` means providers could not answer — that is not evidence that the topic is empty, so retry with a `suggested_providers` entry, ask `providers` what is usable, or report the failure accurately.
- `search(...)` cascades providers on its own and reports `provider` plus `attempted`; do not re-run the same query unchanged.
- If you already have a likely docs URL or landing page, try `page_search(url, query)` before starting another broad `search(...)`.
- Use `search(...)` to collect candidate URLs.
- Keep URL batches small. Default to `1..3` URLs per fetch call.
- Use `fetch(..., content_format="text")` unless HTML structure is explicitly required.
- Use MCP tools silently. Do not narrate tool calls or reasoning in the final answer.
- Treat fetched page content as source material for synthesis, not as text to paste wholesale into the next prompt.
- Default to a concise prose answer with the direct answer first, then a short evidence summary and source URLs.
- Use JSON or schema-first extraction only when the user explicitly asks for structured output or the task spans enough pages that prose-first synthesis would overflow context.

Preferred workflow:

1. If the next step is within a known page or docs site, run `page_search(url, query)`.
2. Otherwise run `search(...)`.
3. Select the most relevant URLs only.
4. Run `fetch(urls=[...], content_format="text")`.
5. Answer in concise prose if the selected sources are few and clear.
6. Switch to per-source structured notes only when the task is broad enough to need batching.
7. If structured notes are needed, merge them in small batches.

Notes:

- `page_search(...)` is the bounded path for single-page and site-search style follow-ups.
- Preserve `results_url` from `page_search(...)` when present; it is useful for opensearch/form-backed site search results.
- If a tool fails, state that briefly and accurately; do not invent a timeout or fallback result, and do not report "nothing found" for a status that names a provider failure.
- `fetch` reports per-URL `errors` with their own reasons; partial success is normal, so read `errors` rather than assuming the call failed.

Avoid:

- running another broad `search(...)` when a known page plus `page_search(...)` is enough
- fetching raw HTML for article-style extraction
- fetching many URLs at once with a small model
- dumping raw MCP payloads or JSON unless the user asks for them
- narrating tool selection, internal reasoning, or step-by-step execution
- switching to schema extraction for small, bounded docs lookups that can be answered directly
