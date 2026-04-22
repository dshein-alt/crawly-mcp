# `crawly` Rebrand, Browser-Mode Split, and Ubuntu Container Release

## Summary
Ship this as three PRs:

1. **Rebrand** the project to `crawly`
2. **Add browser source selection** for host vs. bundled Playwright Chromium
3. **Add the Ubuntu-based container and release CI**

This keeps branding, runtime behavior, and container/registry work independently reviewable and revertable.

## Public Interfaces
- Product/repo/docs/server name: `crawly`
- Python distribution name: `crawly-mcp`
- Python import/package path: `crawly_mcp` (PEP 503-normalized form of the distribution name)
- Console scripts:
  - `crawly-cli` for `search` and `fetch`
  - `crawly-mcp` for serving MCP
- Remove `crawly-cli` and `crawly-mcp` immediately; no compatibility shims
- Keep project version at `0.1.0`
- Add:
  - `PLAYWRIGHT_BROWSER_SOURCE=system|bundled`
  - `CRAWLY_HOST`
  - `CRAWLY_PORT`
- Keep MCP tool names exactly: `search`, `fetch`

## Implementation Changes
### PR 1: Rebrand
- Rename `web-search-mcp` / `web_search_mcp` to `crawly` across metadata, source tree, imports, tests, docs, and server display name.
- Set Python distribution metadata to `crawly-mcp` to avoid the existing PyPI `crawly` name collision; the import package follows the normalized form `crawly_mcp`.
- Add one explicit README note explaining the naming:
  - installable distribution: `crawly-mcp`
  - import package: `crawly_mcp`
  - executables: `crawly-cli`, `crawly-mcp`
- Add reproducible verification for the rename using grep/find checks so reviewers can confirm no old `web-search-mcp*` or `web_search_mcp` names remain in tracked files.
- Update `[Unreleased]` changelog with:
  - `Changed: Rename the project to crawly.`
  - `Changed: Rename the CLI and MCP executables to crawly-cli and crawly-mcp.`

### PR 2: Browser Source Split
- Extend browser startup logic to support:
  - `system`: current host Chromium resolution with `PLAYWRIGHT_CHROMIUM_EXECUTABLE` or PATH
  - `bundled`: launch Playwright Chromium without `executable_path`
- Default to `system` when unset.
- Keep current search/fetch behavior, SSRF policy, timeouts, and provider behavior unchanged.
- Test this PR at the Playwright API boundary:
  - mock `playwright.async_api.async_playwright`
  - assert that bundled mode calls Chromium `launch(...)` without `executable_path`
  - assert that system mode still passes `executable_path`
- Explicitly note in the PR and docs that PR 2 does not prove bundled-mode launch end-to-end; real bundled-browser validation lands in PR 3.
- Update `[Unreleased]` changelog with:
  - `Added: Add PLAYWRIGHT_BROWSER_SOURCE to choose system or bundled Chromium.`

### PR 3: Container and Release CI
- Add a multi-stage `Dockerfile`.
- Base image policy:
  - use the official Playwright Python Ubuntu image
  - pin by full version tag, not digest
  - use a tag like `mcr.microsoft.com/playwright/python:v1.58.0-noble`
  - verify the exact tag exists at PR 3 kickoff before coding
- Builder/runtime strategy:
  - install `uv` in the builder stage
  - run `uv sync --frozen --no-dev`
  - copy the built environment and app into the runtime stage
  - use `COPY --chown=<runtime-user>:<runtime-user>` for app files and venv so runtime ownership is correct
- Runtime user:
  - prefer the Playwright image’s non-root `pwuser`
  - if upstream behavior differs, set an explicit non-root user in the Dockerfile
  - include a smoke test that runs under the non-root runtime user, not only default `docker run`
- Runtime defaults:
  - `PLAYWRIGHT_BROWSER_SOURCE=bundled`
  - `CRAWLY_HOST=0.0.0.0`
  - `CRAWLY_PORT=8000`
  - command should read host/port from env
  - primary interface: HTTP MCP on `streamable-http`
- Security posture:
  - no built-in HTTP auth in v1
  - explicitly document the endpoint as unauthenticated
  - document expected deployment behind localhost, private network, or an auth/TLS reverse proxy
- `.dockerignore`:
  - exclude `.git`, `.venv`, `tests/`, `docs/`, `AGENTS.md`, caches, and local metadata
  - include `.ruff_cache/`, `.pytest_cache/`, and `.mypy_cache/` in the ignore list
- Release workflow:
  - build on PRs and default-branch pushes without publishing
  - publish only on stable tags matching `^v[0-9]+\.[0-9]+\.[0-9]+$`
  - only those same stable tags update `latest`
  - no prerelease publishing and no nightly builds
  - support manual dispatch for rebuild/retry
  - build `linux/amd64` and `linux/arm64`
  - use QEMU + buildx cache and explicitly accept slower arm64 builds on GitHub-hosted runners
  - publish to:
    - `ghcr.io/<github-owner>/crawly-mcp`
    - `<dockerhub-namespace>/crawly-mcp`
  - emit OCI labels plus buildx `--provenance=true --sbom=true`
  - defer signing and vulnerability scan gating to a follow-up
- GHCR visibility:
  - include a one-time manual step to make the first GHCR package public if needed
- Update `[Unreleased]` changelog with:
  - `Added: Add an Ubuntu-based container image with Playwright-managed Chromium.`
  - `Added: Add CRAWLY_HOST and CRAWLY_PORT environment variables for the MCP server.`

## Test Plan
- Rebrand:
  - imports resolve from `crawly_mcp`
  - `crawly-cli --help` and `crawly-mcp --help` work
  - reproducible grep check confirms no `web-search-mcp` or `web_search_mcp` remains in tracked files
- Browser-mode split:
  - default mode is `system`
  - bundled mode skips host executable lookup
  - system mode still honors `PLAYWRIGHT_CHROMIUM_EXECUTABLE`
  - bundled-mode tests mock the Playwright boundary, not internal `BrowserManager` methods
  - real bundled launch verification is deferred to PR 3
- Container CI:
  - build image successfully
  - run container and verify HTTP MCP readiness on port `8000`
  - use the MCP Python SDK `streamable_http` client for smoke tests
  - assert `tools/list` returns exactly `search` and `fetch`
  - call `fetch` against `https://example.com`
  - run a local >1 MiB HTTP fixture and verify `truncated` is populated
  - verify SSRF rejection for a private/container-network target from inside the container runtime path
  - verify provenance is attached with:
    - `docker buildx imagetools inspect --format "{{json .Provenance}}" <image>:<tag>`
  - verify SBOM is attached with a concrete buildx inspection command in the same CI job
- Manual post-release:
  - pull and run `crawly-mcp` from GHCR and Docker Hub
  - run one real DuckDuckGo `search` request outside CI

## Assumptions and Defaults
- `crawly` is the official user-facing name everywhere.
- `crawly-mcp` is the Python distribution name only; the import package is `crawly_mcp` (PEP 503 normalized).
- The rename is immediate and does not include deprecated aliases.
- The version stays `0.1.0`.
- HTTP MCP is the primary container interface; stdio is secondary via command override.
- Container reproducibility comes from `uv.lock` plus an explicit Playwright image version tag, not from digest pinning.
- No nightly images, prerelease images, automatic release artifacts, signing, or scan enforcement in v1.
