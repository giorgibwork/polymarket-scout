# Publish from Termux (phone-friendly)

This avoids manually uploading the hidden `.github/workflows` folder through a mobile file picker. It creates the public repository in **your** authenticated GitHub account and pushes only the scaffold code. The script does not send your GitHub token to ChatGPT.

## Steps

1. Extract `polymarket-scout-github-repo.zip` into a directory in Termux's shared Downloads, then open that extracted folder in Termux. For an archive at `~/storage/downloads/polymarket-scout-github-repo.zip`, one way is:

   ```bash
   pkg install gh git unzip -y
   mkdir -p ~/bots/scout-cloud
   cd ~/bots/scout-cloud
   unzip -o ~/storage/downloads/polymarket-scout-github-repo.zip
   cd polymarket-scout-github-repo
   ```

2. Authenticate locally with GitHub if needed:

   ```bash
   gh auth login
   ```

   Choose GitHub.com and HTTPS, then complete GitHub's browser/device verification. Do not paste an access token into this chat.

3. Publish and request the first workflow run:

   ```bash
   bash scripts/publish-to-github.sh
   ```

   The script requests the GitHub `workflow` permission needed to publish workflow files, then asks you to type `CREATE PUBLIC REPO` before creating a public repository named `polymarket-scout`. If that repository already exists, creation fails rather than overwriting it. You can choose a different name as an argument, for example `bash scripts/publish-to-github.sh polymarket-scout-georgia`.

4. Confirm the repository URL printed by the script. Open its **Actions** tab and inspect the first run. The first run should run the self-test, scan public data, validate SQLite, and upload a `scout-state` artifact. A later run must explicitly restore that artifact. One successful initial upload alone does not prove persistence.

## Important

- The repository is deliberately public to fit the free GitHub-hosted runner path. Source, workflow logs, and artifact contents may be visible publicly. Do not add secrets or personal information.
- The workflow runs nominally every 30 minutes, but GitHub may delay scheduled events.
- The cloud database starts a new history baseline, so it needs at least one hour of locally observed CLOB midpoint history before the current signal rules can qualify a trade. It must not force entries during warm-up.
- The scaffold has not been tested on GitHub Actions yet. Local self-tests and workflow static checks are not a substitute for verifying the remote first run and subsequent restore.
- Official command references: https://cli.github.com/manual/gh_repo_create and https://cli.github.com/manual/gh_auth_refresh
