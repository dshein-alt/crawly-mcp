# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial release.
- Add `PLAYWRIGHT_BROWSER_SOURCE` to choose system or bundled Chromium.
- Add an Ubuntu-based container image with Playwright-managed Chromium.
- Add `CRAWLY_HOST` and `CRAWLY_PORT` environment variables for the MCP server.
- Add runtime logging via loguru with `CRAWLY_LOG_LEVEL` (default `INFO`); output goes to stderr so stdio MCP stays clean.
- Add a project-local launcher script for starting the containerized stdio MCP server from the current checkout.
- Add a project-local launcher script for starting the containerized HTTP MCP server from the current checkout.
- `patchright` as the Playwright engine for stealth patching (replaces stock `playwright`).
- Per-search-provider persistent browser contexts with on-disk profiles under `CRAWLY_PROFILE_DIR`.
- Homepage warm-up hop and randomized jitter on first search per provider.
- Client-hint headers (`sec-ch-ua*`) consistent with the advertised UA.
- Fingerprint canary script (`scripts/fingerprint_check.py`) and a release-gated CI job.
- `CRAWLY_BROWSER_LANG`, `CRAWLY_BROWSER_LOCATION`, and `CRAWLY_BROWSER_VIEWPORT` env vars to tune the browser persona without code changes.
- Age-based profile cleanup at startup (gated by `CRAWLY_PROFILE_CLEANUP_ON_START`, enabled in the Docker image).
- `CRAWLY_USE_PERSISTENT_PROFILES` env var (default `true`) to toggle per-provider persistent search profiles vs ephemeral incognito contexts at runtime.
- `CRAWLY_TRACE_DIR` env var to dump per-search artifacts for network and fingerprint analysis when explicitly enabled.

### Changed

- Rename the project to crawly.
- Rename the CLI and MCP executables to `crawly-cli` and `crawly-mcp`.
- Rename the container image from `crawly` to `crawly-mcp` on GHCR and Docker Hub to match the PyPI distribution name.
- `URLSafetyGuard.pop_blocked_error()` now requires a `Page` argument and tracks blocked requests per page.
- `fetch()` browser contexts now inherit the same browser identity (UA, TZ, client hints) as search contexts; this may alter returned HTML on TZ-aware sites. See the Browser configuration section in the README for the tradeoff.
- Default headless launch switched from legacy `--headless` to `--headless=new`.
- Remove the Xvfb container path and run Chromium in the default headless configuration only.

### Fixed

- Yandex search endpoint now consistently targets `yandex.ru` instead of mixing `yandex.ru` warm-up with `yandex.com` search.

[Unreleased]: https://github.com/dshein-alt/crawly/compare/HEAD...HEAD
