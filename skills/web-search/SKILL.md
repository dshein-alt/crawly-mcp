---
name: web-search
description: Use when a context-limited LLM must search, fetch, and synthesize content from multiple pages with crawly-mcp, especially when `fetch(content_format="text")` and schema-first extraction keep search work within a small context window
---

# Web Search

Use this when a small local LLM needs facts from multiple pages and raw fetch results would overflow context.

For `crawly-mcp`, prefer the built-in bounded path first:

- call `page_search(url, query)` first when the task is really "find this within a known page or docs site"
- call `search(...)` to collect candidate URLs
- call `fetch(urls=[...], content_format="text")` instead of raw HTML
- keep `urls` small, usually `1..3` per fetch for local models
- extract only a fixed JSON schema per page
- reduce extracted records in batches, not all at once

## When To Use

- search + fetch + synthesize across multiple pages
- local or small-context LLMs processing crawled pages
- context overflows after a few fetches
- evidence gathering where you need structured per-source notes

Do not use for single-page questions or when search snippets already answer the question. For a single known page or docs entrypoint, use `page_search(...)` before broader web search.

## Default Workflow

1. Define the final output schema first.
2. Run `search(...)` and keep only the most relevant URLs.
3. Fetch with `content_format="text"`.
4. Map each page into one JSON record matching the schema.
5. Append records to `extracted.jsonl`.
6. Reduce in small batches, then merge rollups if needed.
7. Keep `urls.txt`, `extracted.jsonl`, and any rollups until the answer is verified.

## crawly-mcp Guidance

For this repo and its Docker image:

- Prefer `page_search(url, query)` over `search(...)` + `fetch(...)` when you already know the target page or docs site.
- Prefer `fetch(..., content_format="text")` over post-processing HTML yourself.
- Use `content_format="html"` only when markup structure is the actual task.
- Treat `pages[url]` as bounded source text, not as something to dump straight back into the next prompt.
- If `page_search(...)` returns `mode="opensearch"` or `mode="form"`, keep `results_url` because it identifies the landed search page.

## Why This Works

| Step | Failure if skipped |
|---|---|
| Schema first | Per-page outputs turn into variable-length prose |
| `content_format="text"` | Markup dominates tokens before reasoning starts |
| Per-URL JSON record | Reduce step balloons with free-form summaries |
| JSONL append | Long runs cannot resume cleanly |
| Batched reduce | Loading every record reintroduces overflow |
| Keep intermediates | Wrong final answers are hard to debug |

## Common Mistakes

- Fetching raw HTML for article-style extraction
- Using `search(...)` when a known page plus `page_search(...)` would answer faster
- Sending all fetched pages into one prompt
- Asking the model to “summarize the page” without a schema
- Reducing all extracted records in one pass
- Deleting intermediates before checking the final answer

## Minimal Pattern

```python
import json
from pathlib import Path

SCHEMA = {
    "source_url": "",
    "title": "",
    "date": "",
    "key_facts": [],
}


def append_jsonl(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


urls = Path("urls.txt").read_text(encoding="utf-8").splitlines()

for url in urls:
    payload = mcp_call(
        "fetch",
        {"urls": [url], "content_format": "text"},
    )
    text = payload["pages"].get(url, "")
    record = llm_extract(text, schema=SCHEMA, source_url=url)
    append_jsonl("extracted.jsonl", record)

records = load_jsonl("extracted.jsonl")
rollups = []
for start in range(0, len(records), 10):
    batch = records[start : start + 10]
    rollups.append(reduce_batch(batch))

answer = reduce_batch(rollups) if len(rollups) > 1 else rollups[0]
```

Adapt `mcp_call`, `llm_extract`, and `reduce_batch` to your stack.

## Practical Defaults For Small Models

- fetch one URL at a time
- use `content_format="text"`
- keep extraction schema under 10 fields
- batch reduce at 5 to 10 records
- if overflow persists, reduce URLs per fetch and shrink the extraction schema
