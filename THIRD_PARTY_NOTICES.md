# Third-party notices

This file records third-party code under evaluation for the A-share backtest project. It is an
inventory, not a replacement for an upstream license. As reviewed on 2026-08-30, local glue
adapters cite the reviewed projects, but no upstream source tree is vendored wholesale.
The adapter files and exact adoption mode are recorded in `THIRD_PARTY.yml`.

## HKUDS/Vibe-Trading

- Source: <https://github.com/HKUDS/Vibe-Trading>
- Reviewed revision: [`e90b6c6cd9fea23067a85667e7fbf74f9d73ea48`](https://github.com/HKUDS/Vibe-Trading/tree/e90b6c6cd9fea23067a85667e7fbf74f9d73ea48)
- License: MIT
- Upstream license: <https://github.com/HKUDS/Vibe-Trading/blob/e90b6c6cd9fea23067a85667e7fbf74f9d73ea48/LICENSE>
- Archived exact license: [`third_party/licenses/HKUDS-Vibe-Trading-e90b6c6cd9fea23067a85667e7fbf74f9d73ea48/LICENSE`](third_party/licenses/HKUDS-Vibe-Trading-e90b6c6cd9fea23067a85667e7fbf74f9d73ea48/LICENSE)
- Local glue: bounded candidate generation is optionally runtime-wired and fixture-tested, but its
  production transport is not configured or Live-verified. The strict daily Mootdx acquisition
  shape is fixture-only and is not wired to production runtime. No Vibe indicator code is adopted.
- Distribution requirement: when source or a substantial portion is copied, retain the upstream
  copyright and permission notice with the distributed copy. Record local modifications and the
  pinned revision.

Packaging must include the archived exact license above with any distributed Vibe-derived source.

## moss-site/moss-trade-bot-skills

- Source: <https://github.com/moss-site/moss-trade-bot-skills>
- Reviewed tag and revision: [`v1.0.28` / `1fce09b03151a01ba1dee230466ec64cdd12fda8`](https://github.com/moss-site/moss-trade-bot-skills/tree/1fce09b03151a01ba1dee230466ec64cdd12fda8)
- License: MIT No Attribution (`MIT-0`)
- Upstream license: <https://github.com/moss-site/moss-trade-bot-skills/blob/1fce09b03151a01ba1dee230466ec64cdd12fda8/LICENSE>
- Archived exact license: [`third_party/licenses/moss-trade-bot-skills-1fce09b03151a01ba1dee230466ec64cdd12fda8/LICENSE`](third_party/licenses/moss-trade-bot-skills-1fce09b03151a01ba1dee230466ec64cdd12fda8/LICENSE)
- Local glue: a thin pandas-backed EMA/RSI/MACD differential provider adapted from the published
  formulas. EMA and MACD pass the current A-share comparison gate; RSI is intentionally excluded
  because its seed and zero-loss semantics differ. This adapter is fixture-only and is not wired
  to the signal runtime.
- Distribution requirement: the reviewed license contains no attribution condition. Provenance,
  pinned revision, and modification history are still retained as project policy.

If MOSS source is distributed, preserve the archived exact license as project provenance even
though attribution is not a license condition.

MOSS remains a differential reference, not the authority for the new volatility and channel
definitions. Its ATR/ADX smoothing uses pandas `ewm(span=period)` rather than the project's frozen
Wilder `alpha=1/period` recurrence; its Ichimoku chikou uses a negative shift that would leak future
closes into old rows; and Supertrend still lacks an independently accepted initialization/band
contraction vector. Stochastic, Williams %R and Donchian code may inform test cases, but do not enter
the production runtime.

## bukosabino/ta

- Source: <https://github.com/bukosabino/ta>
- Audited package artifact: PyPI `ta==0.11.0` (2023 sdist), SHA-256
  `de86af43418420bd6b088a2ea9b95483071bf453c522a8441bc2f12bcf8493fd`
- Separate current-source review: [`a890410710a6e483c9ba08da7f3dd5089e4b9dff`](https://github.com/bukosabino/ta/tree/a890410710a6e483c9ba08da7f3dd5089e4b9dff)
- License: MIT
- Current use: source-reviewed test-oracle candidate only; the package is not installed, imported,
  executed, copied, or used as a production dependency.

The 2026 master commit is not the source revision for the 2023 `0.11.0` package and must not be
described as `0.11.0` plus that commit. The immutable sdist hash identifies the package audit;
the commit identifies only the later source review.

Its pandas/NumPy implementation may be useful for future independent float comparisons, but it has different
leading-value, NaN/fill, zero-volume and window-alignment semantics. In particular, its ATR seed
includes its first-row true range and its Donchian band includes the current bar by default. Any
future oracle test must normalize those declared differences; the first-party Decimal formula and
Catalog identity remain authoritative.

## AKShare Push2 protocol reference

- Source: <https://github.com/akfamily/akshare>
- Reviewed revision: [`8e95744b79ae22326308ccd2b4e62650c5b53c55`](https://github.com/akfamily/akshare/tree/8e95744b79ae22326308ccd2b4e62650c5b53c55)
- Reviewed path: `akshare/stock_feature/stock_hist_em.py:stock_zh_a_hist`
- License: MIT
- Archived exact license: [`third_party/licenses/AKShare-8e95744b79ae22326308ccd2b4e62650c5b53c55/LICENSE`](third_party/licenses/AKShare-8e95744b79ae22326308ccd2b4e62650c5b53c55/LICENSE)
- Local glue: the Push2 request protocol only; AKShare is not imported as a runtime dependency.
  The protocol-shaped local adapter is used by acquisition/preparation paths and adds strict A-share
  identity, response hashing, unit checks, independent session/company-action evidence and immutable
  snapshot publication.
- Evidence boundary: fixture-tested; the current direct network probe was disconnected by the remote
  endpoint, so this is not a Live data or snapshot claim. The public undocumented endpoint is not
  authorized for production use.

If this adapted protocol code is distributed, preserve the AKShare copyright and permission notice
and the pinned revision in the distribution's third-party license artifacts.

## pypdfium2 and packaged PDFium

- Locked package: `pypdfium2==5.13.0`.
- Source: <https://github.com/pypdfium2-team/pypdfium2>
- Runtime use: optional embedded-text extraction from announcement PDFs in
  `ashare_lab/adapters/event_sources/document_text.py`; this is not OCR and does not by itself make
  an event historically tradable.
- Declared license set: BSD-3-Clause, Apache-2.0, and packaged dependency-specific licenses.
- Archived exact installed-wheel license tree: [`third_party/licenses/pypdfium2-5.13.0`](third_party/licenses/pypdfium2-5.13.0)
- Artifact boundary: the archived PDFium and dependency notices are the exact license tree shipped
  by the locked macOS arm64 wheel. A Windows, Linux, Intel macOS, or later wheel must archive and
  verify its own matching license tree before distribution.

The entire archived license tree must ship with the corresponding binary wheel or bundled runtime.

## BaoStock

- Locked package: `baostock==0.9.3`.
- Source declared by the package: <http://www.baostock.com>
- Package page: <https://pypi.org/project/baostock/0.9.3/>
- Runtime use: optional Demo acquisition/reference preparation; it is not approved as a production
  provider.
- Package metadata declaration: `BSD License`.
- Archived exact installed metadata: [`third_party/licenses/baostock-0.9.3/METADATA`](third_party/licenses/baostock-0.9.3/METADATA)

The locked wheel contains no exact license file. The metadata declaration must not be replaced by a
generic BSD template. BaoStock is release-blocked until the exact upstream license and the data
service's redistribution/commercial-use terms are obtained and approved.

## mootdx

- Candidate package: `mootdx==0.11.7` (not installed or distributed).
- The repository `LICENSE` is standard MIT, but the same upstream README states that the project
  is for study/exchange only and must not be used commercially.
- Because those statements conflict, and the underlying TDX data-use terms also require review,
  the package is blocked from runtime adoption pending legal approval. The local client Protocol
  and fake-client tests do not install or execute mootdx.

## Governance

The machine-readable source of truth is [`THIRD_PARTY.yml`](THIRD_PARTY.yml). Any copied module must
appear there before merge, together with its adoption mode, forbidden semantics, tests, and rollback
path. Exact Vibe, MOSS, and AKShare license texts and the locked macOS arm64 pypdfium2/PDFium license
bundle are archived. BaoStock license/data terms, mootdx's conflicting terms, Live acceptance, and
production data authorization remain unresolved release gates. Legal or procurement review may
impose additional requirements before commercial distribution.
