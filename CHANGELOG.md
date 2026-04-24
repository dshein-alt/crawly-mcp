# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-04-24

Add initial `page_search` tool implementation.

### Added

- `page_search(url, query)` MCP tool: three-tier cascade over Algolia DocSearch, OpenSearch descriptor, Readthedocs API, generic GET forms, and find-in-page text fallback. Returns a `mode` discriminator, ordered `attempted` list, and up to 5 result snippets with optional result URLs.
- `crawly-cli page-search --url URL --query TEXT` subcommand mirroring the MCP tool.

### Changed

- Promote `httpx` from a transitive dependency to an explicit project dependency (used by `page_search` for Algolia and Readthedocs API calls).
- Make the bundled web-search skill and Continue prompt default to silent tool use and concise prose answers, reserving JSON-style extraction for larger multi-page runs.
- Expand MCP server and tool metadata so clients prefer silent tool use, prose answers, `page_search` on known sites, and `fetch(..., content_format="text")` for readable follow-up fetches.

### Fixed

- Preserve `results_url` for `page_search` results returned from OpenSearch and generic form tiers.
- Ignore non-query controls when detecting `page_search` search forms and preserve the original parameter-name casing.
- Wait briefly for client-side search pages to populate results before `page_search` snapshots the HTML.
- Extract linked results from OpenSearch and generic form search result pages before falling back to full-query text snippets.
- Add the missing MIT `LICENSE` file and document the project license in package metadata and README.
- Copy `LICENSE` into the Docker builder stage before `uv sync` so container builds satisfy packaged license-file metadata.

## [0.1.1] - 2026-04-23

Regular updates and fixes.

### Added

### Changed
- Run the release fingerprint canary against bundled Chromium and stop treating CI software WebGL renderers as a failure.

### Fixed
- Docker build CI pipeline

## [0.1.0] - 2026-04-23

First viable release.

### Added

### Changed

### Fixed
