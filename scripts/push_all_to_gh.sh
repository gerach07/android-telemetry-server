#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="${GIT_REMOTE:-origin}"
if [ "$#" -gt 0 ]; then
  COMMIT_MESSAGE="$*"
else
  COMMIT_MESSAGE="Update repository"
fi

cd "$ROOT"

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "Remote '$REMOTE' is not configured." >&2
  exit 1
fi

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
  echo "Unable to determine the current branch." >&2
  exit 1
fi

echo "[*] Staging all changes..."

EXCLUDES=()
while IFS= read -r gitdir; do
  nested_root="${gitdir%/.git}"
  nested_root="${nested_root#$ROOT/}"
  [ -n "$nested_root" ] || continue
  EXCLUDES+=(":(exclude)$nested_root" ":(exclude)$nested_root/**")
done < <(find "$ROOT" -path "$ROOT/.git" -prune -o -type d -name .git -print)

if [ "${#EXCLUDES[@]}" -gt 0 ]; then
  echo "Embedded git repositories found:" >&2
  printf '%s\n' "${EXCLUDES[@]}" \
    | sed 's/^:(exclude)//' \
    | sort -u \
    | sed 's/^/  /' >&2
  echo "This script can only push the outer repository." >&2
  echo "If you want those directories included, remove their inner .git folders or convert them to submodules first." >&2
  exit 1
fi

git add -A -- . "${EXCLUDES[@]}"

if git diff --cached --quiet; then
  echo "[*] No staged changes to commit."
else
  echo "[*] Creating commit: $COMMIT_MESSAGE"
  git commit -m "$COMMIT_MESSAGE"
fi

echo "[*] Pushing $BRANCH to $REMOTE..."
git push "$REMOTE" "$BRANCH"

echo "[*] Done."