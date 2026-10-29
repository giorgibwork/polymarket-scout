# Polymarket Scout (paper-only)

A read-only Polymarket public-data scanner with a simulated paper ledger. It does not place orders and must not be treated as evidence of profitability.

## GitHub Actions runner

Code release: **v2.0.1**. Ledger/model namespace remains **v2.0** intentionally, so this hotfix can continue managing existing v2.0 paper positions without merging their results with legacy records.

The workflow in `.github/workflows/scout.yml` runs manually or approximately every 30 minutes. GitHub may delay scheduled runs. It runs the built-in self-test, scans public market data, validates and checkpoints SQLite, then uploads the database as a workflow artifact. On later runs, it searches recent completed workflow runs for the newest downloadable artifact with a valid Scout database.

### Important limitations

- **Repository visibility:** for a free recurring workflow, make this repository **public**. The source and workflow logs are public. The SQLite artifact contains market observations and paper positions, not wallet credentials, but artifacts are accessible to people with repository access. Do not add secrets or personal data.
- **Persistence:** first run starts a new database. Later runs restore a validated `scout-state` artifact from a recent completed run. Artifact retention is configured for 90 days. If completed prior runs exist but none of the 20 most recent contains a valid downloadable database, the workflow fails closed rather than silently discarding history. Always verify the Actions logs for restore and upload confirmations.
- **Scheduling:** `*/30 * * * *` means a nominal 30-minute cadence, not an exact guarantee. GitHub can delay scheduled workflows, especially during load. Use the Actions tab to inspect run history and logs.
- **No trade execution:** the bot is paper-only. Do not add wallet keys or order-placement code.
- **Not financial advice:** the strategy is unproven. Visible order-book simulation does not guarantee real fills or profitability.

## Setup

**Phone/Termux path:** see [`TERMUX_SETUP.md`](TERMUX_SETUP.md). The included `scripts/publish-to-github.sh` can create and publish the public repo from your own authenticated Termux session after an explicit confirmation, including the hidden workflow directory. It does not send tokens to ChatGPT.

1. Create a **public** GitHub repository named `polymarket-scout`.
2. Upload the contents of this folder to the repository root, including the hidden `.github/workflows/scout.yml` file.
3. Open **Actions** and enable workflows if GitHub prompts you.
4. Open **Actions → Polymarket Scout → Run workflow** to test a run manually.
5. Verify the self-test and scan outcome, confirm that `scout-state` is uploaded, and run it again to prove database restoration. The second run should explicitly report restoring and validating SQLite. A successful upload on only the first run is not proof of persistence.

## Local use

Python 3.10+; standard library only:

```bash
python polymarket_scout.py --version
python polymarket_scout.py self-test
python polymarket_scout.py scan
python polymarket_scout.py status
```

The default database path is `~/.polymarket_scout/scout.sqlite3`. The GitHub workflow uses `state/scout.sqlite3` and stores it as an artifact instead of committing database files into Git history.
