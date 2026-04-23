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

### Changed

- Rename the project to crawly.
- Rename the CLI and MCP executables to `crawly-cli` and `crawly-mcp`.
- Rename the container image from `crawly` to `crawly-mcp` on GHCR and Docker Hub to match the PyPI distribution name.

### Fixed

[Unreleased]: https://github.com/dshein-alt/crawly/compare/HEAD...HEAD
