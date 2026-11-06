# Investigate Yandex CAPTCHA blocks

Status: implemented on branch `yandex-captcha` (0.4.5). Findings below; the
original brief follows, kept as written.

Yandex returned `blocked` for both an English and a Russian query on
2026-11-06. Investigate the browser-verification flow and correct any browser
lifecycle or classification problems that prevent ordinary search from completing.
Preserve accurate blocked outcomes when Yandex requires human verification.

## Reproduction

Run queries sequentially with the same provider profile and disable cascading:

```sh
CRAWLY_PROFILE_DIR=/tmp/crawly-yandex-repro \
CRAWLY_TRACE_DIR=/tmp/crawly-yandex-traces \
CRAWLY_SEARCH_PROVIDER_ORDER=yandex \
CRAWLY_SEARCH_MAX_PROVIDERS=1 \
PLAYWRIGHT_BROWSER_SOURCE=bundled \
uv run crawly-cli search --provider yandex --context 'python'

CRAWLY_PROFILE_DIR=/tmp/crawly-yandex-repro \
CRAWLY_TRACE_DIR=/tmp/crawly-yandex-traces \
CRAWLY_SEARCH_PROVIDER_ORDER=yandex \
CRAWLY_SEARCH_MAX_PROVIDERS=1 \
PLAYWRIGHT_BROWSER_SOURCE=bundled \
uv run crawly-cli search --provider yandex --context 'документация Python'
```

The captured run used one browser manager and a fresh persistent Yandex profile
across both queries. Both returned `status=blocked`, `page_kind=captcha`:

| Query | Landed path | Title | Visible page |
| --- | --- | --- | --- |
| `python` | `/showcaptchafast` | `Верификация` | Browser verification; asks the visitor to wait for automatic navigation |
| `документация Python` | `/showcaptcha` | `Вы не робот?` | Explicit Yandex SmartCaptcha prompt |

The first request performed the homepage warm-up. The second reused the same
context. Both traces report `webdriver=false`, JavaScript enabled, locale
`ru-RU`, and timezone `Europe/Moscow`. These properties do not establish why
Yandex blocked the requests.

Local evidence from verification is under `/tmp/crawly_verify/yandex/`:
`results.json`, `run.log`, and `traces/` with page HTML, screenshots, browser
fingerprints, and navigation records. Temporary artifacts may require recapture.

## Investigation

- Compare automatic verification with an explicit CAPTCHA. Search currently
  reads and classifies immediately after navigation; the bounded challenge wait
  in `challenge.py` is used for fetch. Determine whether a bounded wait lets
  Yandex's automatic verification complete before reporting a block.
- Inspect the homepage warm-up and navigation records for unfinished checks or
  failed script requests. Keep the existing profile throughout the comparison.
- Check actual Chromium version against the user agent and client hints, and
  verify that browser-source selection applies to persistent contexts as well
  as fetch contexts. Treat identity inconsistencies as hypotheses until measured.
- If automatic verification still leads to SmartCaptcha, preserve the block
  and cascade to another configured provider. Document the observed limitation
  and available operator configuration.

## Acceptance criteria

- Add fixtures for automatic verification and explicit SmartCaptcha, with tests
  covering the applicable classification and any settle behavior.
- Keep any automatic-verification wait within the provider and cascade budgets;
  test a transition to results and a check that never finishes.
- Preserve `blocked` for a human-verification wall and keep it distinct from
  `no_results`. Confirm another provider can answer through the cascade.
- Preserve profile continuity and SSRF checks. Follow the project's existing
  approach of no CAPTCHA solving or proxy rotation.
- Report live observations with queries and counts. Claim improved Yandex
  availability only when a repeatable comparison supports it.
- Keep implementation and changelog changes separate from the Google follow-up.

## Findings (2026-11-06, measured)

Reproduced on this host with a fresh profile and `MAX_PROVIDERS=1`:
`status=blocked`, `page_kind=captcha`, landed `https://yandex.ru/showcaptchafast`.

Yandex serves two walls, and they are not the same page:

| Path | Title | Mechanism | Self-resolves? |
| --- | --- | --- | --- |
| `/showcaptchafast` | `Верификация` | `tmgrdfrend` browser check: JS computes `fingerprint`/`chstate`, auto-submits `#tmgrdfrend-form` to `/checkcaptchafast`, which redirects to `retpath` (the real results URL, base64 in the query) | **Yes, at t≈1s** |
| `/showcaptcha?cc=1` | `Вы не робот?` | Yandex SmartCaptcha `#checkbox-captcha-form`, `CheckboxCaptcha` widget, submit `#js-button` | **No** (observed 20s, no transition) |

The first is a **transient state that forks**, not a wall in its own right.
Sampling the page at fixed offsets after navigation (`scripts/yandex_wall_probe.py`;
each run is its own control, because variants run at different moments are
confounded by Yandex's time-varying rate limits):

```
0.0:auto/0  1.0:res/0   2.0:res/82   <- fork to results
0.0:auto/0  1.0:sma/0   2.0:sma/0  ... 20.0:sma/0   <- fork to SmartCaptcha
```

- 5 cold-profile runs landing on `showcaptchafast`: 1 forked to results, 4 to
  SmartCaptcha. The fork completes by t≈1.0s in every case.
- Warm profile: 19/20 runs land directly on results; the one wall (first request
  on a fresh profile) resolved at t=1.0s to 81 anchors.
- At t=1.0s on a resolved run the URL is already `/search` with **0 anchors**;
  anchors appear by t=2.0s. A settle that stops at the URL change extracts
  nothing and reports `unavailable`.
- Warm-up is load-bearing: dropping it turned 1/5 clean runs into 4/5
  SmartCaptcha. It lands on `dzen.ru` (yandex.ru redirects there) and still
  establishes the identity the search page accepts.

**Root cause.** `_run_search` reads and classifies immediately after navigation.
The bounded settle window in `challenge.py` is used only for fetch. So search
classifies `showcaptchafast` at t=0, mid-check, and reports `blocked` on a page
that was about to answer — and names the transient interstitial as the wall.

**Fix.** Settle a wall on the search path, in the module that already owns the
bounded settle window, and make it budget-aware against the provider deadline
(the `OPAQUE_RESOLVE_CLEANUP_SECONDS` precedent). Stop early on a wall that
requires a human click, so `blocked` stays fast and names the wall the provider
actually ended on. No CAPTCHA solving, no proxy rotation, no clicking.

## Verification (0.4.5)

Paired A/B, baseline `ccd91e5` against this branch, interleaved order, identical
cold profiles, `MAX_PROVIDERS=1`, three queries (`scripts/ab_yandex.sh 6`):

| build | ok | blocked |
| --- | --- | --- |
| baseline | 2 | 4 |
| this branch | **6** | **0** |

In the four rounds where the baseline reported `blocked`, the branch returned 5
result URLs on the same query in the same round; the two rounds the baseline
answered were answered by the branch too. No regression in either direction.

Precision, 12 rapid searches on one profile to draw the real wall: 11 returned
5 URLs, and the one that reached `/showcaptcha?cc=1` was reported `blocked`,
`page_kind=captcha`, `final_url` naming the checkbox wall — not a false `ok`.
The block now names the wall the provider ended on rather than the transient
interstitial.

Cascade: with `yandex,duckduckgo` and a walled Yandex, DuckDuckGo answers
(unit test); with the fix in place 8/8 runs were answered by Yandex itself.

Latency: clean searches 3.2–4.8s end to end, unchanged — a page that is not
mid-flight is returned from the settle without a poll, so nothing is spent.

Tests: 323 pass. `ruff check` and `ruff format --check` clean.
