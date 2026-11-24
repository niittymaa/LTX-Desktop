#!/usr/bin/env bash
# update-and-run.sh
# Updates LTX Desktop from upstream (resolving any merge conflicts via AI CLI),
# rebuilds the frontend, Electron shell, and Python runtime if needed,
# replaces /Applications/LTX Desktop.app atomically, and launches the app.

set -euo pipefail

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colors & logging ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BLUE='\033[0;34m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  RED='\033[0;31m'
  BOLD='\033[1m'
  NC='\033[0m'
else
  BLUE='' GREEN='' YELLOW='' RED='' BOLD='' NC=''
fi

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Defaults & CLI options ────────────────────────────────────────────────────
SKIP_UPDATE=false
SKIP_BUILD=false
REBUILD_PYTHON=false
NO_LAUNCH=false
AI_CLI_CHOICE="${AI_CLI:-}"
UPSTREAM_REMOTE="upstream"
UPSTREAM_BRANCH=""

usage() {
  cat <<'EOF'
Usage: ./update-and-run.sh [options]

Options:
  --skip-update         Skip git fetch and merge from upstream
  --skip-build          Skip dependency install, build, and /Applications replacement
  --rebuild-python      Force rebuilding python-embed/ even if dependency stamp matches
  --no-launch           Do not launch /Applications/LTX Desktop.app after build
  --ai <agy|claude|codex>
                        AI CLI to use for conflict resolution (default: $AI_CLI or auto)
  --upstream <remote>   Git remote to pull updates from (default: upstream)
  --branch <branch>     Upstream branch to merge (default: remote HEAD or main)
  -h, --help            Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-update)
      SKIP_UPDATE=true
      ;;
    --skip-build)
      SKIP_BUILD=true
      ;;
    --rebuild-python)
      REBUILD_PYTHON=true
      ;;
    --no-launch)
      NO_LAUNCH=true
      ;;
    --ai)
      if [[ $# -lt 2 ]]; then
        err "--ai requires a value (agy, claude, or codex)"
        exit 1
      fi
      AI_CLI_CHOICE="$2"
      shift
      ;;
    --upstream)
      if [[ $# -lt 2 ]]; then
        err "--upstream requires a remote name"
        exit 1
      fi
      UPSTREAM_REMOTE="$2"
      shift
      ;;
    --branch)
      if [[ $# -lt 2 ]]; then
        err "--branch requires a branch name"
        exit 1
      fi
      UPSTREAM_BRANCH="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
  shift
done

# ── Cleanup trap ──────────────────────────────────────────────────────────────
STAGE_APP=""
cleanup() {
  if [ -n "$STAGE_APP" ] && [ -e "$STAGE_APP" ]; then
    rm -rf "$STAGE_APP" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ── AI Conflict Resolution ────────────────────────────────────────────────────
select_ai_cli() {
  local preferred="$1"
  if [ -n "$preferred" ]; then
    case "$preferred" in
      agy|claude|codex)
        if command -v "$preferred" >/dev/null 2>&1; then
          echo "$preferred"
          return 0
        fi
        err "Requested AI CLI '$preferred' was not found in PATH."
        return 1
        ;;
      *)
        err "Unsupported AI CLI '$preferred'. Choose one of: agy, claude, codex."
        return 1
        ;;
    esac
  fi

  local candidate
  for candidate in agy claude codex; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done

  err "No supported AI CLI found in PATH (tried: agy, claude, codex)."
  return 1
}

resolve_conflicts_with_ai() {
  local context="$1"
  local conflicts="$2"
  local ai_cli
  ai_cli="$(select_ai_cli "$AI_CLI_CHOICE")"

  warn "Conflicts detected during ${context}:"
  echo "$conflicts" | sed 's/^/  - /'
  info "Invoking '${ai_cli}' to resolve conflicts automatically..."

  local prompt
  prompt=$(cat <<EOF
You are resolving Git merge conflicts in the repository at ${SCRIPT_DIR} during ${context}.

Conflicted files:
${conflicts}

Instructions:
1. Inspect \`git status\` and read each conflicted file listed above.
2. Resolve every conflict cleanly, preserving both upstream changes and any intentional local customizations.
3. Ensure zero conflict markers (<<<<<<<, =======, >>>>>>>) remain in any file.
4. Stage every resolved file with \`git add <file>\` (or \`git rm <file>\` if a file was intentionally deleted).
5. Do NOT run \`git push\`, do NOT abort the merge, and do NOT modify unrelated files.
EOF
)

  case "$ai_cli" in
    agy)
      agy -p --dangerously-skip-permissions "$prompt"
      ;;
    claude)
      claude -p --dangerously-skip-permissions "$prompt"
      ;;
    codex)
      codex exec --dangerously-bypass-approvals-and-sandbox "$prompt"
      ;;
  esac

  # Strict post-AI verification
  local remaining_unresolved
  remaining_unresolved="$(git diff --name-only --diff-filter=U)"
  if [ -n "$remaining_unresolved" ]; then
    err "AI conflict resolution left unresolved files:"
    echo "$remaining_unresolved" | sed 's/^/  - /' >&2
    exit 1
  fi

  local marker_found=false
  local file
  while IFS= read -r file; do
    [ -z "$file" ] && continue
    if [ -f "$file" ]; then
      if grep -nE '^(<<<<<<< |=======[[:space:]]*$|>>>>>>> )' "$file" >/dev/null 2>&1; then
        err "Leftover conflict markers detected in '$file':"
        grep -nE '^(<<<<<<< |=======[[:space:]]*$|>>>>>>> )' "$file" >&2 || true
        marker_found=true
      fi
    fi
  done <<< "$conflicts"

  if [ "$marker_found" = true ]; then
    err "Aborting: conflict markers still remain after AI resolution."
    exit 1
  fi

  ok "All conflicts resolved and verified cleanly."
}

# ── Step A: Upstream update ───────────────────────────────────────────────────
update_from_upstream() {
  info "Checking for updates from remote '${UPSTREAM_REMOTE}'..."

  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    err "Not inside a Git repository: $SCRIPT_DIR"
    exit 1
  fi

  if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
    err "Git remote '${UPSTREAM_REMOTE}' does not exist."
    exit 1
  fi

  local stashed=false
  if ! git diff --quiet || ! git diff --cached --quiet; then
    info "Working tree has local tracked changes; stashing before update..."
    git stash push -m "update-and-run auto-stash $(date +%Y-%m-%d_%H:%M:%S)"
    stashed=true
  fi

  git fetch "$UPSTREAM_REMOTE"

  if [ -z "$UPSTREAM_BRANCH" ]; then
    UPSTREAM_BRANCH="$(git symbolic-ref --short "refs/remotes/${UPSTREAM_REMOTE}/HEAD" 2>/dev/null | sed "s#^${UPSTREAM_REMOTE}/##" || true)"
    if [ -z "$UPSTREAM_BRANCH" ]; then
      UPSTREAM_BRANCH="main"
    fi
  fi

  local upstream_ref="${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"
  if ! git rev-parse --verify "$upstream_ref" >/dev/null 2>&1; then
    err "Upstream ref '${upstream_ref}' not found."
    if [ "$stashed" = true ]; then
      warn "Restoring stashed changes..."
      git stash pop || true
    fi
    exit 1
  fi

  if git merge-base --is-ancestor "$upstream_ref" HEAD; then
    ok "Already up to date with ${upstream_ref}."
  else
    info "Merging ${upstream_ref} into $(git branch --show-current)..."
    set +e
    git merge --no-edit "$upstream_ref"
    local merge_status=$?
    set -e

    if [ "$merge_status" -ne 0 ]; then
      local conflicts
      conflicts="$(git diff --name-only --diff-filter=U)"
      if [ -z "$conflicts" ]; then
        err "git merge failed without standard unmerged file conflicts (exit $merge_status)."
        exit "$merge_status"
      fi

      resolve_conflicts_with_ai "merge with ${upstream_ref}" "$conflicts"

      local merge_head
      merge_head="$(git rev-parse --git-path MERGE_HEAD)"
      if [ -f "$merge_head" ]; then
        git commit --no-edit
      fi
    fi
    ok "Merged ${upstream_ref}."
  fi

  if [ "$stashed" = true ]; then
    info "Restoring stashed local changes..."
    set +e
    git stash pop
    local pop_status=$?
    set -e

    if [ "$pop_status" -ne 0 ]; then
      local pop_conflicts
      pop_conflicts="$(git diff --name-only --diff-filter=U)"
      if [ -z "$pop_conflicts" ]; then
        err "git stash pop failed without standard unmerged file conflicts (exit $pop_status)."
        exit "$pop_status"
      fi
      resolve_conflicts_with_ai "git stash pop" "$pop_conflicts"
    fi
    ok "Stashed changes restored."
  fi
}

# ── Step B: Dependencies & python-embed ───────────────────────────────────────
compute_python_stamp() {
  shasum -a 256 \
    backend/uv.lock \
    backend/pyproject.toml \
    backend/.python-version \
    scripts/prepare-python.sh \
    scripts/prebuild-mps-sdpa-ext.sh \
    | shasum -a 256 | awk '{print $1}'
}

prepare_dependencies() {
  info "Installing Node dependencies with pnpm..."
  pnpm install
  ok "Node dependencies installed."

  local stamp_file="python-embed/.deps-stamp"
  local current_stamp
  current_stamp="$(compute_python_stamp)"

  local need_python_rebuild=false
  if [ "$REBUILD_PYTHON" = true ]; then
    info "Forcing python-embed/ rebuild (--rebuild-python)."
    need_python_rebuild=true
  elif [ ! -d "python-embed" ] || [ ! -x "python-embed/bin/python3" ]; then
    info "python-embed/ not found or incomplete; preparing standalone Python environment..."
    need_python_rebuild=true
  elif [ ! -f "$stamp_file" ] || [ "$(cat "$stamp_file")" != "$current_stamp" ]; then
    info "Python dependencies or build scripts changed; rebuilding python-embed/..."
    need_python_rebuild=true
  else
    ok "python-embed/ is up to date (skipping rebuild)."
  fi

  if [ "$need_python_rebuild" = true ]; then
    bash scripts/prepare-python.sh
    printf '%s\n' "$current_stamp" > "$stamp_file"
    ok "python-embed/ prepared and stamped."

    if [ -d "backend/.venv" ]; then
      info "Syncing backend/.venv with updated dependencies..."
      (cd backend && uv sync --frozen --extra desktop --extra dev)
      ok "backend/.venv synced."
    fi
  fi
}

# ── Step C: Build & package unpacked macOS app ────────────────────────────────
build_app() {
  info "Building frontend, Electron main/preload, and Remote UI..."
  pnpm run build:frontend
  ok "Frontend and Electron build complete."

  info "Packaging unpacked macOS application bundle..."
  CSC_IDENTITY_AUTO_DISCOVERY=false pnpm exec electron-builder --mac --dir -c.mac.notarize=false -c.mac.hardenedRuntime=false
  ok "Unpacked macOS bundle ready."
}

find_built_app() {
  local arch_dir="release/mac-arm64/LTX Desktop.app"
  local x64_dir="release/mac/LTX Desktop.app"
  if [ -d "$arch_dir" ]; then
    echo "$arch_dir"
    return 0
  elif [ -d "$x64_dir" ]; then
    echo "$x64_dir"
    return 0
  fi
  err "Built app bundle not found at '$arch_dir' or '$x64_dir'."
  exit 1
}

# ── Step D: Atomic replacement in /Applications ───────────────────────────────
install_app() {
  local built_app="$1"
  local app_name="LTX Desktop"
  local target_app="/Applications/${app_name}.app"
  local stage_app="/Applications/.${app_name}.app.new.$$"
  local backup_app="/Applications/.${app_name}.app.old.$$"
  STAGE_APP="$stage_app"

  # Quit running app gracefully if active
  if pgrep -x "$app_name" >/dev/null 2>&1; then
    info "Quitting running '${app_name}'..."
    osascript -e "tell application \"${app_name}\" to quit" >/dev/null 2>&1 || true
    local waited=0
    while pgrep -x "$app_name" >/dev/null 2>&1 && [ "$waited" -lt 8 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if pgrep -x "$app_name" >/dev/null 2>&1; then
      warn "Force closing remaining '${app_name}' process..."
      pkill -x "$app_name" >/dev/null 2>&1 || true
      sleep 2
    fi
  fi

  local sudo_prefix=()
  if [ ! -w "/Applications" ]; then
    warn "/Applications is not directly writable; sudo will be used."
    sudo_prefix=(sudo)
  fi

  info "Copying '${built_app}' to '${target_app}'..."
  "${sudo_prefix[@]+"${sudo_prefix[@]}"}" ditto "$built_app" "$stage_app"
  "${sudo_prefix[@]+"${sudo_prefix[@]}"}" xattr -dr com.apple.quarantine "$stage_app" 2>/dev/null || true

  local had_existing=false
  if [ -e "$target_app" ]; then
    had_existing=true
    "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$target_app" "$backup_app"
  fi

  if ! "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$stage_app" "$target_app"; then
    err "Failed to move staged app into '${target_app}'."
    if [ "$had_existing" = true ] && [ -e "$backup_app" ]; then
      warn "Rolling back previous '${target_app}'..."
      "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$backup_app" "$target_app" || true
    fi
    exit 1
  fi
  STAGE_APP=""

  if [ -e "$backup_app" ]; then
    "${sudo_prefix[@]+"${sudo_prefix[@]}"}" rm -rf "$backup_app"
  fi

  ok "Installed '${target_app}'."
}

# ── Step E: Launch app ────────────────────────────────────────────────────────
launch_app() {
  local target_app="/Applications/LTX Desktop.app"
  if [ ! -d "$target_app" ]; then
    err "Cannot launch: '${target_app}' does not exist."
    exit 1
  fi
  info "Launching '${target_app}'..."
  open -a "$target_app"
  ok "LTX Desktop launched."
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo -e "${BOLD}=== LTX Desktop: Update, Build & Run ===${NC}"

if [ "$SKIP_UPDATE" = false ]; then
  update_from_upstream
else
  info "Skipping upstream update (--skip-update)."
fi

if [ "$SKIP_BUILD" = false ]; then
  prepare_dependencies
  build_app
  BUILT_APP="$(find_built_app)"
  install_app "$BUILT_APP"
else
  info "Skipping build and installation (--skip-build)."
fi

if [ "$NO_LAUNCH" = false ]; then
  launch_app
else
  info "Skipping launch (--no-launch)."
fi

ok "Done."
