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
- Optional Xvfb mode via `CRAWLY_USE_XVFB` and the `run-with-xvfb.sh` entrypoint.
- Fingerprint canary script (`scripts/fingerprint_check.py`) and a release-gated CI job.
- `TZ` env var support for browser context timezone (default `America/New_York`).
- Age-based profile cleanup at startup (gated by `CRAWLY_PROFILE_CLEANUP_ON_START`, enabled in the Docker image).

### Changed

- Rename the project to crawly.
- Rename the CLI and MCP executables to `crawly-cli` and `crawly-mcp`.
- Rename the container image from `crawly` to `crawly-mcp` on GHCR and Docker Hub to match the PyPI distribution name.
- `URLSafetyGuard.pop_blocked_error()` now requires a `Page` argument and tracks blocked requests per page.
- `fetch()` browser contexts now inherit the same stealth identity (UA, TZ, client hints) as search contexts; this may alter returned HTML on TZ-aware sites. See the Stealth configuration section in the README for the tradeoff.
- Default headless launch switched from legacy `--headless` to `--headless=new`.

### Fixed

- Yandex search endpoint now consistently targets `yandex.ru` instead of mixing `yandex.ru` warm-up with `yandex.com` search.

[Unreleased]: https://github.com/dshein-alt/crawly/compare/HEAD...HEAD
