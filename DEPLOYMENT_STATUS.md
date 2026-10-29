# Deployment status

This folder is a prepared repository scaffold (code release v2.0.1; ledger/model namespace remains v2.0 to preserve open v2.0 paper positions). It has not yet been pushed to GitHub or run in GitHub Actions. Local validation has passed, but remote execution remains unverified.

Before relying on it, verify in the Actions logs that:

1. the self-test passes;
2. a market scan completes or reports a clear public-API failure;
3. a `scout-state` artifact is uploaded and passes SQLite integrity validation;
4. a later run restores a validated database artifact;
5. state continuity is visible in quote-history row counts and timestamps.

The workflow checks up to 20 recent completed runs for a valid database artifact and fails closed rather than silently resetting the ledger if prior runs exist but no valid database can be restored. The first-ever run starts fresh; persistence is not considered verified until a second run explicitly restores the first run's artifact. The scanner is intentionally read-only and paper-only. No remote repository has been created by this scaffold.
