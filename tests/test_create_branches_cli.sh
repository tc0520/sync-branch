#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
MULTI_TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "$MULTI_TMP"' EXIT

git_cmd() {
    git "$@"
}

make_project() {
    local name="$1"
    mkdir -p "$TMP/remotes" "$TMP/work"
    git_cmd init --bare "$TMP/remotes/${name}.git" >/dev/null
    git_cmd clone "$TMP/remotes/${name}.git" "$TMP/seed-${name}" >/dev/null 2>&1
    (
        cd "$TMP/seed-${name}"
        git_cmd checkout -b main >/dev/null
        git_cmd config user.email test@example.com
        git_cmd config user.name "Test User"
        printf '%s\n' "$name" > README.md
        git_cmd add README.md
        git_cmd commit -m initial >/dev/null
        git_cmd push -u origin main >/dev/null 2>&1
    )
    git_cmd -C "$TMP/remotes/${name}.git" symbolic-ref HEAD refs/heads/main
    git_cmd clone "$TMP/remotes/${name}.git" "$TMP/work/${name}" >/dev/null 2>&1
    git_cmd -C "$TMP/work/${name}" config user.email test@example.com
    git_cmd -C "$TMP/work/${name}" config user.name "Test User"
}

make_project repo_one
make_project repo_dirty
make_project repo_remote
make_project repo_local_only
make_project repo_switch

make_multi_project() {
    local root="$1" name="$2"
    mkdir -p "$root/remotes" "$root/work"
    git_cmd init --bare "$root/remotes/${name}.git" >/dev/null
    git_cmd clone "$root/remotes/${name}.git" "$root/seed-${name}" >/dev/null 2>&1
    (
        cd "$root/seed-${name}"
        git_cmd checkout -b main >/dev/null
        git_cmd config user.email test@example.com
        git_cmd config user.name "Test User"
        printf '%s\n' "$name" > README.md
        git_cmd add README.md
        git_cmd commit -m initial >/dev/null
        git_cmd push -u origin main >/dev/null 2>&1
    )
    git_cmd -C "$root/remotes/${name}.git" symbolic-ref HEAD refs/heads/main
    git_cmd clone "$root/remotes/${name}.git" "$root/work/${name}" >/dev/null 2>&1
    git_cmd -C "$root/work/${name}" config user.email test@example.com
    git_cmd -C "$root/work/${name}" config user.name "Test User"
}

mkdir -p "$MULTI_TMP/first" "$MULTI_TMP/second"
make_multi_project "$MULTI_TMP/first" repo_shared
make_multi_project "$MULTI_TMP/second" repo_shared

SYNC_BASE_DIR="$MULTI_TMP/first/work;$MULTI_TMP/second/work" "$ROOT/sync-branches.sh" --create dev_first_cli <<'EOF'
repo_shared
EOF

[ "$(git_cmd -C "$MULTI_TMP/first/work/repo_shared" branch --show-current)" = "dev_first_cli" ]
[ "$(git_cmd -C "$MULTI_TMP/second/work/repo_shared" branch --show-current)" = "main" ]

printf 'old work\n' > "$TMP/work/repo_dirty/scratch.txt"

SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --create dev_cli --push <<'EOF'
repo_one
repo_dirty
EOF

[ "$(git_cmd -C "$TMP/work/repo_one" branch --show-current)" = "dev_cli" ]
[ "$(git_cmd -C "$TMP/work/repo_dirty" branch --show-current)" = "dev_cli" ]
git_cmd -C "$TMP/work/repo_one" rev-parse --verify origin/dev_cli >/dev/null
git_cmd -C "$TMP/work/repo_dirty" rev-parse --verify origin/dev_cli >/dev/null
[ "$(git_cmd -C "$TMP/work/repo_one" rev-parse --abbrev-ref '@{u}')" = "origin/dev_cli" ]

if [ -e "$TMP/work/repo_dirty/scratch.txt" ]; then
    echo "scratch.txt should stay in stash, not new branch worktree" >&2
    exit 1
fi
git_cmd -C "$TMP/work/repo_dirty" stash list | grep -q 'sync-branches-create: main'

SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --create dev_cli <<'EOF'
repo_one
EOF

[ "$(git_cmd -C "$TMP/work/repo_one" branch --show-current)" = "dev_cli" ]

(
    cd "$TMP/work/repo_remote"
    git_cmd checkout -b dev_remote_existing >/dev/null
    git_cmd push -u origin dev_remote_existing >/dev/null 2>&1
    git_cmd checkout main >/dev/null
    git_cmd branch -D dev_remote_existing >/dev/null
)

SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --create dev_remote_existing <<'EOF'
repo_remote
EOF

[ "$(git_cmd -C "$TMP/work/repo_remote" branch --show-current)" = "dev_remote_existing" ]
[ "$(git_cmd -C "$TMP/work/repo_remote" rev-parse --abbrev-ref '@{u}')" = "origin/dev_remote_existing" ]

SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --create dev_local_only <<'EOF'
repo_local_only
EOF

[ "$(git_cmd -C "$TMP/work/repo_local_only" branch --show-current)" = "dev_local_only" ]
if git_cmd -C "$TMP/work/repo_local_only" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    echo "local-only created branch should not track origin/main" >&2
    exit 1
fi

git_cmd clone "$TMP/remotes/repo_switch.git" "$TMP/peer-repo_switch" >/dev/null 2>&1
(
    cd "$TMP/peer-repo_switch"
    git_cmd config user.email test@example.com
    git_cmd config user.name "Test User"
    git_cmd checkout -b dev_switch >/dev/null
    printf 'target remote\n' > target.txt
    git_cmd add target.txt
    git_cmd commit -m target-remote >/dev/null
    git_cmd push -u origin dev_switch >/dev/null 2>&1
    git_cmd checkout main >/dev/null
    printf 'main remote\n' > main-switch.txt
    git_cmd add main-switch.txt
    git_cmd commit -m main-remote >/dev/null
    git_cmd push origin main >/dev/null 2>&1
)

printf 'old switch work\n' > "$TMP/work/repo_switch/scratch-switch.txt"

SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --switch dev_switch <<'EOF'
repo_switch
EOF

[ "$(git_cmd -C "$TMP/work/repo_switch" branch --show-current)" = "dev_switch" ]
[ -f "$TMP/work/repo_switch/target.txt" ]
[ -f "$TMP/work/repo_switch/main-switch.txt" ]
if [ -e "$TMP/work/repo_switch/scratch-switch.txt" ]; then
    echo "scratch-switch.txt should stay in stash, not target branch worktree" >&2
    exit 1
fi
git_cmd -C "$TMP/work/repo_switch" stash list | grep -q 'sync-branches-switch: main'

if SYNC_BASE_DIR="$TMP/work" "$ROOT/sync-branches.sh" --switch missing_target <<'EOF'
repo_switch
EOF
then
    echo "switching a missing target branch should fail" >&2
    exit 1
fi

echo "cli branch tests passed"
