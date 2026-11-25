#!/usr/bin/env bash
# update-and-run.sh
# Updates LTX Desktop from upstream (resolving any merge conflicts via AI CLI),
# rebuilds the frontend, Electron shell, and Python runtime if needed,
# replaces /Applications/LTX Desktop.app atomically without hidden dotfiles,
# refreshes macOS LaunchServices/Spotlight registration, and launches the app.

set -euo pipefail

export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="LTX Desktop"
BUNDLE_ID="com.lightricks.ltx-desktop"
TARGET_APP="/Applications/${APP_NAME}.app"
STAMP_DIR="$HOME/Library/Application Support/LTXDesktop"
STAMP_FILE="${STAMP_DIR}/installed-build.json"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

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

QUIET=false

info() { [ "$QUIET" = true ] || echo -e "${BLUE}[INFO]${NC} $*"; }
ok()   { [ "$QUIET" = true ] || echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Defaults & CLI options ────────────────────────────────────────────────────
SKIP_UPDATE=false
SKIP_BUILD=false
REBUILD_PYTHON=false
NO_LAUNCH=false
KEEP_RELEASE=false
REFRESH_REGISTRATION=false
CHECK_ONLY=false
INSTALL_HOOKS=false
AI_CLI_CHOICE="${AI_CLI:-}"
UPSTREAM_REMOTE="upstream"
UPSTREAM_BRANCH=""

usage() {
  cat <<'EOF'
Usage: ./update-and-run.sh [options]

Options:
  --local               Sync local repo features to /Applications/LTX Desktop.app (alias for --skip-update)
  --skip-update         Skip git fetch and merge from upstream
  --skip-build          Skip dependency install, build, and /Applications replacement
  --rebuild-python      Force rebuilding python-embed/ even if dependency stamp matches
  --no-launch           Do not launch /Applications/LTX Desktop.app after build
  --keep-release        Keep release/mac-arm64/LTX Desktop.app after installing to /Applications
  --refresh-registration
                        Refresh macOS LaunchServices and Spotlight registration without rebuilding or launching
  --check               Verify /Applications/LTX Desktop.app is present, uniquely registered, and up to date with repo
  --install-hooks       Install non-blocking git post-commit and post-merge reminder hooks
  --quiet               Reduce output verbosity (useful with --check)
  --ai <agy|claude|codex>
                        AI CLI to use for conflict resolution (default: $AI_CLI or auto)
  --upstream <remote>   Git remote to pull updates from (default: upstream)
  --branch <branch>     Upstream branch to merge (default: remote HEAD or main)
  -h, --help            Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --local|--skip-update)
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
    --keep-release)
      KEEP_RELEASE=true
      ;;
    --refresh-registration)
      REFRESH_REGISTRATION=true
      ;;
    --check)
      CHECK_ONLY=true
      ;;
    --install-hooks)
      INSTALL_HOOKS=true
      ;;
    --quiet)
      QUIET=true
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
STAGE_DIR=""
cleanup() {
  if [ -n "$STAGE_DIR" ] && [ -e "$STAGE_DIR" ]; then
    chmod -R u+w "$STAGE_DIR" 2>/dev/null || true
    rm -rf "$STAGE_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ── App-source fingerprint & build stamp ──────────────────────────────────────
compute_app_source_fingerprint() {
  /usr/bin/python3 - "$SCRIPT_DIR" <<'PY'
import hashlib
import os
import subprocess
import sys

repo = sys.argv[1]
paths = [
    "frontend",
    "electron",
    "backend",
    "shared",
    "resources",
    "package.json",
    "electron-builder.yml",
    "settings.json",
    "vite.config.ts",
    "vite.remote.config.ts",
]

out = subprocess.check_output(
    ["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths]
)
files = sorted(set(p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p))

def excluded(rel: str) -> bool:
    parts = rel.split("/")
    if rel.startswith("backend/tests/") or "/tests/" in rel:
        return True
    if "__pycache__" in parts or ".venv" in parts:
        return True
    if rel.endswith(".pyc"):
        return True
    for prefix in ("backend/outputs/", "backend/models/", "backend/tmp/", "backend/generated/"):
        if rel.startswith(prefix):
            return True
    return False

h = hashlib.sha256()
for rel in files:
    if excluded(rel):
        continue
    full = os.path.join(repo, rel)
    h.update(rel.encode("utf-8", "surrogateescape"))
    h.update(b"\0")
    if os.path.isfile(full):
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    elif os.path.islink(full):
        h.update(b"SYMLINK:")
        h.update(os.readlink(full).encode("utf-8", "surrogateescape"))
    else:
        h.update(b"MISSING")
    h.update(b"\0")

print(h.hexdigest())
PY
}

write_installed_build_stamp() {
  local fingerprint
  fingerprint="$(compute_app_source_fingerprint)"
  local git_commit
  git_commit="$(git rev-parse HEAD 2>/dev/null || echo "unknown")"
  local git_branch
  git_branch="$(git branch --show-current 2>/dev/null || echo "detached")"
  local git_dirty="false"
  if [ -n "$(git status --porcelain -- frontend electron backend shared resources package.json electron-builder.yml settings.json vite.config.ts vite.remote.config.ts 2>/dev/null)" ]; then
    git_dirty="true"
  fi
  local installed_at
  installed_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  mkdir -p "$STAMP_DIR"
  /usr/bin/python3 - "$STAMP_FILE" "$fingerprint" "$git_commit" "$git_branch" "$git_dirty" "$installed_at" "$TARGET_APP" "$SCRIPT_DIR" <<'PY'
import json
import sys

stamp_path, fp, commit, branch, dirty, ts, app_path, repo_path = sys.argv[1:9]
data = {
    "sourceFingerprint": fp,
    "gitCommit": commit,
    "gitBranch": branch,
    "gitDirty": dirty == "true",
    "installedAt": ts,
    "appPath": app_path,
    "repoPath": repo_path,
}
with open(stamp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  ok "Recorded build stamp at '${STAMP_FILE}'."
}

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
ensure_release_noindex() {
  mkdir -p release
  touch release/.metadata_never_index
}

build_app() {
  ensure_release_noindex

  info "Building frontend, Electron main/preload, and Remote UI..."
  pnpm run build:frontend
  ok "Frontend and Electron build complete."

  info "Packaging unpacked macOS application bundle..."
  CSC_IDENTITY_AUTO_DISCOVERY=false pnpm exec electron-builder --mac --dir -c.mac.notarize=false -c.mac.hardenedRuntime=false
  ensure_release_noindex
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

# ── Step D: Atomic replacement outside /Applications & macOS registration ─────
stop_running_app() {
  if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    info "Quitting running '${APP_NAME}'..."
    osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
    local waited=0
    while pgrep -x "$APP_NAME" >/dev/null 2>&1 && [ "$waited" -lt 8 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
      warn "Force closing remaining '${APP_NAME}' process..."
      pkill -x "$APP_NAME" >/dev/null 2>&1 || true
      sleep 2
    fi
  fi

  # Also wait briefly for bundled backend server process to finish releasing ports 41954/41956
  local backend_pattern="/Applications/${APP_NAME}.app/Contents/Resources/backend/ltx2_server.py"
  if pgrep -f "$backend_pattern" >/dev/null 2>&1; then
    local bwait=0
    while pgrep -f "$backend_pattern" >/dev/null 2>&1 && [ "$bwait" -lt 6 ]; do
      sleep 1
      bwait=$((bwait + 1))
    done
    if pgrep -f "$backend_pattern" >/dev/null 2>&1; then
      warn "Terminating lingering bundled backend process..."
      pkill -f "$backend_pattern" >/dev/null 2>&1 || true
      sleep 1
    fi
  fi
}

cleanup_stale_ls_registrations() {
  if [ ! -x "$LSREGISTER" ]; then
    return 0
  fi

  local stale_paths
  stale_paths="$(/usr/bin/python3 - "$LSREGISTER" "$BUNDLE_ID" "$TARGET_APP" <<'PY'
import re
import subprocess
import sys

lsregister, bundle_id, target_app = sys.argv[1:4]
target_prefix = target_app.rstrip("/") + "/"
seen = set()

def check_block(lines: list[str]) -> None:
    ident = None
    path = None
    for line in lines:
        if line.startswith("identifier:"):
            ident = line.split(":", 1)[1].strip()
        elif line.startswith("path:"):
            raw = line.split(":", 1)[1].strip()
            path = re.sub(r"\s+\(0x[0-9a-fA-F]+\)$", "", raw)
    if not ident or not path:
        return
    if ident == bundle_id or ident.startswith(bundle_id + "."):
        if path != target_app and not path.startswith(target_prefix):
            if path not in seen:
                seen.add(path)
                print(path)

dump = subprocess.check_output([lsregister, "-dump"]).decode("utf-8", "replace")
block: list[str] = []
for line in dump.splitlines():
    if line.startswith("----"):
        if block:
            check_block(block)
            block = []
    else:
        block.append(line)
if block:
    check_block(block)
PY
)"

  if [ -n "$stale_paths" ]; then
    local stale_path
    while IFS= read -r stale_path; do
      [ -z "$stale_path" ] && continue
      "$LSREGISTER" -u "$stale_path" >/dev/null 2>&1 || true
    done <<< "$stale_paths"
  fi
}

cleanup_release_registration() {
  local rel_app
  for rel_app in \
    "${SCRIPT_DIR}/release/mac-arm64/${APP_NAME}.app" \
    "${SCRIPT_DIR}/release/mac/${APP_NAME}.app"
  do
    if [ -x "$LSREGISTER" ]; then
      "$LSREGISTER" -u "$rel_app" >/dev/null 2>&1 || true
    fi
    if [ "$KEEP_RELEASE" = false ] && [ -e "$rel_app" ]; then
      info "Removing duplicate build bundle '${rel_app}'..."
      rm -rf "$rel_app"
    fi
  done
  cleanup_stale_ls_registrations
}

refresh_macos_app_registration() {
  if [ ! -d "$TARGET_APP" ]; then
    err "Cannot refresh registration: '${TARGET_APP}' does not exist."
    exit 1
  fi

  cleanup_stale_ls_registrations

  info "Refreshing macOS LaunchServices and Spotlight registration for '${TARGET_APP}'..."
  if [ -w "$TARGET_APP" ]; then
    touch "$TARGET_APP"
  else
    sudo touch "$TARGET_APP"
  fi

  if [ -x "$LSREGISTER" ]; then
    "$LSREGISTER" -f "$TARGET_APP" >/dev/null 2>&1 || "$LSREGISTER" -f "$TARGET_APP"
    "$LSREGISTER" -gc >/dev/null 2>&1 || true
  else
    warn "lsregister not found at '${LSREGISTER}'; skipping LaunchServices refresh."
  fi

  if command -v mdimport >/dev/null 2>&1; then
    mdimport "$TARGET_APP" >/dev/null 2>&1 || true
    if command -v mdfind >/dev/null 2>&1; then
      local md_wait=0
      while [ "$md_wait" -lt 30 ]; do
        if [ "$(mdfind "kMDItemCFBundleIdentifier == '${BUNDLE_ID}'" 2>/dev/null || true)" = "$TARGET_APP" ]; then
          break
        fi
        sleep 0.5
        md_wait=$((md_wait + 1))
      done
    fi
  fi

  ok "macOS registration refreshed."
}

install_app() {
  local built_app="$1"
  STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ltx-desktop-install.XXXXXX")"
  local stage_app="${STAGE_DIR}/${APP_NAME}.app"
  local backup_app="${STAGE_DIR}/${APP_NAME}.old.app"

  stop_running_app

  local sudo_prefix=()
  if [ ! -w "/Applications" ]; then
    warn "/Applications is not directly writable; sudo will be used."
    sudo_prefix=(sudo)
  fi

  info "Staging '${built_app}' outside /Applications..."
  ditto "$built_app" "$stage_app"
  xattr -dr com.apple.quarantine "$stage_app" 2>/dev/null || true

  info "Installing into '${TARGET_APP}'..."
  local had_existing=false
  if [ -e "$TARGET_APP" ]; then
    had_existing=true
    "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$TARGET_APP" "$backup_app"
  fi

  if ! "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$stage_app" "$TARGET_APP"; then
    err "Failed to move staged app into '${TARGET_APP}'."
    if [ "$had_existing" = true ] && [ -e "$backup_app" ]; then
      warn "Rolling back previous '${TARGET_APP}'..."
      "${sudo_prefix[@]+"${sudo_prefix[@]}"}" mv "$backup_app" "$TARGET_APP" || true
    fi
    exit 1
  fi

  chmod -R u+w "$STAGE_DIR" 2>/dev/null || true
  rm -rf "$STAGE_DIR"
  STAGE_DIR=""

  local ro_dir
  for ro_dir in "${TARGET_APP}/Contents/Resources/backend" "${TARGET_APP}/Contents/Resources/python"; do
    if [ -d "$ro_dir" ]; then
      "${sudo_prefix[@]+"${sudo_prefix[@]}"}" chmod -R a-w "$ro_dir"
    fi
  done

  ok "Installed '${TARGET_APP}'."

  cleanup_release_registration
  refresh_macos_app_registration
  write_installed_build_stamp
}

# ── Step E: Launch app ────────────────────────────────────────────────────────
launch_app() {
  if [ ! -d "$TARGET_APP" ]; then
    err "Cannot launch: '${TARGET_APP}' does not exist."
    exit 1
  fi
  info "Launching '${TARGET_APP}'..."
  open -a "$TARGET_APP"
  ok "LTX Desktop launched."
}

# ── Check mode (--check) ──────────────────────────────────────────────────────
check_installed_app() {
  local ok_flag=true

  if [ ! -d "$TARGET_APP" ] || [ ! -f "${TARGET_APP}/Contents/Info.plist" ]; then
    err "Missing or incomplete app bundle at '${TARGET_APP}'."
    ok_flag=false
  else
    ok "Bundle exists: ${TARGET_APP}"
    if codesign --verify --deep --strict "$TARGET_APP" >/dev/null 2>&1; then
      ok "Code signature valid: ${TARGET_APP}"
    else
      err "Code signature invalid for '${TARGET_APP}' (e.g. runtime files such as .pyc modified inside the bundle)."
      ok_flag=false
    fi
  fi

  local rel_app
  for rel_app in \
    "${SCRIPT_DIR}/release/mac-arm64/${APP_NAME}.app" \
    "${SCRIPT_DIR}/release/mac/${APP_NAME}.app"
  do
    if [ -e "$rel_app" ]; then
      err "Duplicate build bundle present outside /Applications: ${rel_app}"
      ok_flag=false
    fi
  done

  if command -v swift >/dev/null 2>&1; then
    local ws_out
    ws_out="$(swift -e '
import Foundation
import AppKit
let urls = NSWorkspace.shared.urlsForApplications(withBundleIdentifier: "com.lightricks.ltx-desktop")
for u in urls { print(u.path) }
' 2>/dev/null || true)"
    if [ "$ws_out" != "$TARGET_APP" ]; then
      err "LaunchServices/NSWorkspace resolution mismatch (expected solely '${TARGET_APP}'):"
      if [ -z "$ws_out" ]; then
        err "  (none registered)"
      else
        echo "$ws_out" | sed 's/^/  - /' >&2
      fi
      ok_flag=false
    else
      ok "LaunchServices resolves solely: ${TARGET_APP}"
    fi
  elif [ -x "$LSREGISTER" ]; then
    local ls_dump
    ls_dump="$("$LSREGISTER" -dump | awk -v id="$BUNDLE_ID" '
      /^----/{
        if (buf ~ ("identifier:[[:space:]]+" id "\n")) print buf
        buf=""
      }
      {buf=buf $0 "\n"}
      END{if (buf ~ ("identifier:[[:space:]]+" id "\n")) print buf}
    ' | grep '^path:' | sed 's/^path:[[:space:]]*//; s/ (0x[0-9a-fA-F]*)$//')"
    if [ "$ls_dump" != "$TARGET_APP" ]; then
      err "LaunchServices paths mismatch (expected solely '${TARGET_APP}'):"
      echo "$ls_dump" | sed 's/^/  - /' >&2
      ok_flag=false
    else
      ok "LaunchServices resolves solely: ${TARGET_APP}"
    fi
  fi

  if command -v mdfind >/dev/null 2>&1; then
    local md_out
    md_out="$(mdfind "kMDItemCFBundleIdentifier == '${BUNDLE_ID}'" 2>/dev/null || true)"
    if [ "$md_out" != "$TARGET_APP" ]; then
      err "Spotlight index mismatch (expected solely '${TARGET_APP}'):"
      if [ -z "$md_out" ]; then
        err "  (not indexed by Spotlight)"
      else
        echo "$md_out" | sed 's/^/  - /' >&2
      fi
      ok_flag=false
    else
      ok "Spotlight indexes solely: ${TARGET_APP}"
    fi
  fi

  if [ ! -f "$STAMP_FILE" ]; then
    err "Build sync stamp missing at '${STAMP_FILE}'."
    ok_flag=false
  else
    local expected_fp
    expected_fp="$(compute_app_source_fingerprint)"
    local installed_fp
    installed_fp="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sourceFingerprint",""))' "$STAMP_FILE" 2>/dev/null || true)"
    if [ -z "$installed_fp" ] || [ "$installed_fp" != "$expected_fp" ]; then
      err "Installed app is out of date with current repo app sources."
      err "  Installed fingerprint: ${installed_fp:-<none>}"
      err "  Repo fingerprint:      ${expected_fp}"
      ok_flag=false
    else
      ok "Installed app matches current repo app-source fingerprint (${expected_fp:0:12})."
    fi
  fi

  if [ "$ok_flag" = false ]; then
    return 1
  fi
  return 0
}

# ── Git hooks installer (--install-hooks) ─────────────────────────────────────
install_git_hooks() {
  local hooks_dir
  hooks_dir="$(git rev-parse --git-path hooks)"
  mkdir -p "$hooks_dir"

  local begin_marker="# BEGIN LTX-DESKTOP APP SYNC HOOK"
  local end_marker="# END LTX-DESKTOP APP SYNC HOOK"

  local hook_block
  hook_block=$(cat <<'EOF'
# BEGIN LTX-DESKTOP APP SYNC HOOK
(
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  if [ -x "$repo_root/update-and-run.sh" ]; then
    if ! "$repo_root/update-and-run.sh" --check --quiet >/dev/null 2>&1; then
      echo "[LTX Desktop] Reminder: /Applications/LTX Desktop.app is out of sync or needs registration refresh."
      echo "[LTX Desktop] Run 'pnpm app:sync' (or './update-and-run.sh --local') to update it."
    fi
  fi
) || true
# END LTX-DESKTOP APP SYNC HOOK
EOF
)

  local hook_name
  for hook_name in post-commit post-merge; do
    local hook_path="${hooks_dir}/${hook_name}"
    if [ ! -e "$hook_path" ]; then
      printf '#!/usr/bin/env bash\n\n%s\n' "$hook_block" > "$hook_path"
      chmod +x "$hook_path"
      ok "Installed '${hook_path}'."
    elif grep -qF "$begin_marker" "$hook_path"; then
      /usr/bin/python3 - "$hook_path" "$begin_marker" "$end_marker" "$hook_block" <<'PY'
import sys

path, begin_m, end_m, block = sys.argv[1:5]
with open(path, "r", encoding="utf-8") as f:
    text = f.read()

start = text.index(begin_m)
end = text.index(end_m, start) + len(end_m)
new_text = text[:start].rstrip("\n") + ("\n\n" if start > 0 else "") + block + text[end:]
if not new_text.endswith("\n"):
    new_text += "\n"
with open(path, "w", encoding="utf-8") as f:
    f.write(new_text)
PY
      chmod +x "$hook_path"
      ok "Updated '${hook_path}'."
    else
      if grep -qE '^[[:space:]]*exit([[:space:]]|$)' "$hook_path"; then
        warn "Existing custom hook '${hook_path}' contains an 'exit' statement; skipping auto-append to avoid altering control flow."
        warn "Add './update-and-run.sh --check --quiet' manually if desired."
      else
        printf '\n%s\n' "$hook_block" >> "$hook_path"
        chmod +x "$hook_path"
        ok "Appended reminder hook to existing '${hook_path}'."
      fi
    fi
  done
}

# ── Main ──────────────────────────────────────────────────────────────────────
if [ "$INSTALL_HOOKS" = true ]; then
  install_git_hooks
  if [ "$CHECK_ONLY" = false ] && [ "$REFRESH_REGISTRATION" = false ] && [ "$SKIP_BUILD" = true -o "$SKIP_UPDATE" = true ]; then
    :
  elif [ "$CHECK_ONLY" = false ] && [ "$REFRESH_REGISTRATION" = false ]; then
    exit 0
  fi
fi

if [ "$CHECK_ONLY" = true ]; then
  check_installed_app
  exit $?
fi

if [ "$REFRESH_REGISTRATION" = true ]; then
  cleanup_release_registration
  refresh_macos_app_registration
  exit 0
fi

[ "$QUIET" = true ] || echo -e "${BOLD}=== LTX Desktop: Update, Build & Run ===${NC}"

if [ "$SKIP_UPDATE" = false ]; then
  update_from_upstream
else
  info "Skipping upstream update (--skip-update/--local)."
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
