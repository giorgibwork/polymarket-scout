# Deployment status

**VERIFIED** — 2026-10-29

Code release: **v2.0.1**. Ledger/model namespace: **v2.0** (preserves continuity for existing v2.0 paper positions).

## Verified workflow runs

| Run | Trigger | Self-test | Scan | SQLite integrity | Artifact uploaded | State restored |
|-----|---------|-----------|------|------------------|-------------------|----------------|
| [40943078874](https://github.com/giorgibwork/polymarket-scout/actions/runs/40943078874) | workflow_dispatch | PASS | PASS (100 markets, 77 binary, 154 books, 77 quotes) | PASS (quick_check=ok) | PASS (31 KB, ID 12928580308) | N/A (first run) |
| [40943167720](https://github.com/giorgibwork/polymarket-scout/actions/runs/40943167720) | workflow_dispatch | PASS | PASS (100 markets, 77 binary, 154 books, 77 quotes) | PASS (quick_check=ok) | PASS (59 KB, ID 12928610289) | **PASS** (restored from run 40943078874) |

## State continuity evidence

- Run 2 explicitly restored and validated SQLite state from run 40943078874 (log: "Restored and validated SQLite state from run 40943078874")
- Quote snapshots accumulated: 154 total (77 + 77)
- Scan runs recorded: 2 (both status=OK)
- Signal log entries: 154
- No silent database resets; fail-closed restoration logic works

## Critical defect fixed

- **market_trade_ready()**: Removed auto-reject on `restricted=True`. Gamma's `restricted` field indicates SOME jurisdictions may be restricted, not that ALL are blocked. The official geographic-check endpoint is separate. Fail-closed checks retained for `active`, `closed`, `accepting_orders`, `enable_order_book`, `archived`.
- Regression test added in `self_test()` verifying `restricted=True/False` both return `(True, "ready")`.

## Remaining limitations

- Repository must be **public** for free recurring GitHub Actions (current: public ✓)
- Scheduled runs (`*/30 * * * *`) are nominal; GitHub may delay during load
- First hour of scans produces zero entries (expected warmup: requires 1h local CLOB midpoint history)
- Strategy is an unproven hypothesis; no profitability claim
- Paper-only: no wallet, no real orders, no API credentials used
- No paid APIs or infrastructure; free-only by default

## Next verification milestones

1. Verify scheduled runs execute (~30 min cadence) via Actions tab
2. Accumulate ≥30 days of scan data or ≥200 closed market outcomes for baseline evaluation
3. Compare momentum strategy against no-trade and alternative baselines with full cost accounting