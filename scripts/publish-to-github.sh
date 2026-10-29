#!/data/data/com.termux/files/usr/bin/bash
# Publish this prepared scaffold from Termux using the user's own GitHub auth.
# This script intentionally creates a PUBLIC repo only after an explicit prompt.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REPO_NAME="${1:-polymarket-scout}"

for cmd in gh git; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'Missing command: %s\nInstall with: pkg install gh git unzip -y\n' "$cmd" >&2
    exit 2
  fi
done

if [ ! -f "polymarket_scout.py" ] || [ ! -f ".github/workflows/scout.yml" ] || [ ! -f ".gitignore" ]; then
  echo 'ERROR: run this script from the extracted repository scaffold; required files are missing.' >&2
  exit 2
fi

if ! gh auth status >/dev/null 2>&1; then
  echo 'GitHub CLI is not authenticated. Run: gh auth login' >&2
  exit 2
fi

OWNER="$(gh api user --jq '.login')"
FULL_NAME="${OWNER}/${REPO_NAME}"

cat <<EOF
Target: ${FULL_NAME}
Visibility: PUBLIC
Code/workflow logs and uploaded database artifacts may be visible to anyone.
The database is not committed to Git; only the workflow artifact is used for continuity.
No wallet credentials are required or used. The bot does not place real orders.

EOF
read -r -p 'To authorize creating this PUBLIC repository, type CREATE PUBLIC REPO: ' CONFIRM
if [ "$CONFIRM" != 'CREATE PUBLIC REPO' ]; then
  echo 'Cancelled; no remote repository was created.'
  exit 0
fi

# GitHub requires the workflow scope when pushing .github/workflows files with
# CLI OAuth authentication. This asks GitHub to grant the scope locally; no token
# is printed or sent to ChatGPT.
echo 'Checking/refreshing the GitHub workflow permission...'
gh auth refresh -h github.com -s workflow

if git remote get-url origin >/dev/null 2>&1; then
  echo 'ERROR: this folder already has an origin remote. Refusing to change or overwrite it.' >&2
  git remote -v >&2
  exit 3
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init --initial-branch=main
fi
git branch -M main

if ! git config user.name >/dev/null; then
  git config user.name "$(gh api user --jq '.name // .login')"
fi
if ! git config user.email >/dev/null; then
  USER_ID="$(gh api user --jq '.id')"
  git config user.email "${USER_ID}+${OWNER}@users.noreply.github.com"
fi

# Stage only known scaffold files so unrelated local files can never be pushed.
git add .gitignore README.md TERMUX_SETUP.md DEPLOYMENT_STATUS.md polymarket_scout.py .github/workflows/scout.yml scripts/publish-to-github.sh
if ! git rev-parse --verify HEAD >/dev/null 2>&1 || ! git diff --cached --quiet; then
  git commit -m 'Prepare Polymarket Scout paper-only cloud runner'
fi

# gh repo create fails safely if the target already exists; it does not overwrite it.
gh repo create "$FULL_NAME" \
  --public \
  --source "$ROOT" \
  --remote origin \
  --push \
  --description 'Read-only Polymarket public-data Scout with a simulated paper ledger'

printf '\nRepository created and code pushed: https://github.com/%s\n' "$FULL_NAME"
echo 'Requesting the first manual workflow run...'
if gh workflow run scout.yml --repo "$FULL_NAME" --ref main; then
  echo 'Workflow dispatch was accepted. This confirms the request, not that the scan has completed.'
  echo "Inspect runs: gh run list --repo ${FULL_NAME} --workflow scout.yml"
else
  echo 'Repository push succeeded, but manual dispatch was not accepted.' >&2
  echo "Open https://github.com/${FULL_NAME}/actions, enable Actions if prompted, then run Polymarket Scout manually." >&2
fi
