#!/bin/bash
#
# sync-branches.sh — 批量同步多个项目的指定分支
#
# 用途：测试同学要求把某些项目的某个分支更新到包含主分支最新代码时，
#       自动完成「stash 当前改动 -> 切目标分支 -> 拉最新 -> 合并主分支 ->
#       推送 -> 切回原分支 -> 恢复 stash」的全流程。
#
# 用法：
#   ./sync-branches.sh                # 交互式粘贴「项目：分支」列表，Ctrl-D 结束
#   ./sync-branches.sh < list.txt     # 也可以从文件读入
#
# 输入格式（中英文冒号均可，空行忽略）：
#   mix_ads_web：dev_ws_api_product
#   mix_ads_ws：dev_ws_api_product
#   rpc_process：dev_ws_api_product
#
# 有冲突的项目会保留冲突现场停在目标分支上（不切回、不弹 stash），
# 等你手动解决后自行 push / checkout / stash pop。

set -u

# 项目根目录：默认脚本所在目录，可用环境变量 SYNC_BASE_DIR 覆盖，
# 例如 SYNC_BASE_DIR=~/projects ./sync-branches.sh
WWW_DIR="${SYNC_BASE_DIR:-$(cd "$(dirname "$0")" && pwd)}"
if [ ! -d "$WWW_DIR" ]; then
    echo "项目根目录不存在: $WWW_DIR" >&2
    exit 1
fi
STASH_TAG="sync-branches-auto"

# ---------- 输出模式 ----------
# SYNC_PORCELAIN=1 时输出机器可读格式（供 sync-branches-ui.py 解析）：
#   @@LOG|<项目>|<级别>|<消息>
#   @@RESULT|<项目>|<ok/conflict/error>|<消息>
PORCELAIN="${SYNC_PORCELAIN:-0}"

if [ -t 1 ] && [ "$PORCELAIN" != "1" ]; then
    C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
    C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

log_line() { # log_line <级别> <项目> <消息>
    if [ "$PORCELAIN" = "1" ]; then
        printf '@@LOG|%s|%s|%s\n' "$2" "$1" "$3"
    else
        case "$1" in
            ok)   echo "${C_GREEN}[$2]${C_RESET} $3" ;;
            warn) echo "${C_YELLOW}[$2]${C_RESET} $3" ;;
            err)  echo "${C_RED}[$2]${C_RESET} $3" ;;
            *)    echo "${C_BLUE}[$2]${C_RESET} $3" ;;
        esac
    fi
}
info()  { log_line info "$1" "$2"; }
ok()    { log_line ok   "$1" "$2"; }
warn()  { log_line warn "$1" "$2"; }
err()   { log_line err  "$1" "$2"; }

# ---------- 汇总（macOS bash 3.2 无关联数组，用平行数组） ----------
SUMMARY_PROJ=()
SUMMARY_STATE=()   # ok / conflict / error
SUMMARY_MSG=()

add_summary() {
    SUMMARY_PROJ+=("$1")
    SUMMARY_STATE+=("$2")
    SUMMARY_MSG+=("$3")
    if [ "$PORCELAIN" = "1" ]; then
        printf '@@RESULT|%s|%s|%s\n' "$1" "$2" "$3"
    fi
}

# ---------- 读取输入 ----------
if [ -t 0 ]; then
    echo "请粘贴「项目：分支」列表（中英文冒号均可），输入完按 Ctrl-D 结束："
fi
RAW_INPUT="$(cat)"

# 统一全角冒号为半角，去掉 \r
INPUT="$(printf '%s\n' "$RAW_INPUT" | sed -e 's/：/:/g' -e 's/\r$//')"

ENTRIES=()
while IFS= read -r line; do
    # 去首尾空白
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in
        \#*) continue ;;
    esac
    if ! printf '%s' "$line" | grep -q ':'; then
        err "输入" "无法解析该行（缺少冒号），已跳过: $line"
        continue
    fi
    ENTRIES+=("$line")
done <<EOF
$INPUT
EOF

if [ "${#ENTRIES[@]}" -eq 0 ]; then
    err "输入" "没有解析到任何「项目:分支」条目，退出。"
    exit 1
fi

# ---------- 工具函数 ----------

# 识别项目主分支：优先 origin/HEAD，其次 origin/master、origin/main
detect_main_branch() {
    local head_ref
    head_ref="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null || true)"
    if [ -n "$head_ref" ]; then
        printf '%s' "${head_ref#refs/remotes/origin/}"
        return 0
    fi
    if git show-ref --verify --quiet refs/remotes/origin/master; then
        printf 'master'; return 0
    fi
    if git show-ref --verify --quiet refs/remotes/origin/main; then
        printf 'main'; return 0
    fi
    return 1
}

# 当前是否处于未完成的合并/冲突状态
in_merge_state() {
    [ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ]
}

# ---------- 主流程 ----------
process_project() {
    local proj="$1" target="$2"
    local dir="$WWW_DIR/$proj"

    echo ""
    echo "${C_BOLD}========== $proj -> $target ==========${C_RESET}"

    if [ ! -d "$dir" ]; then
        err "$proj" "目录不存在: $dir"
        add_summary "$proj" "error" "目录不存在"
        return
    fi
    if ! git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
        err "$proj" "不是 git 仓库: $dir"
        add_summary "$proj" "error" "不是 git 仓库"
        return
    fi

    cd "$dir" || { add_summary "$proj" "error" "无法进入目录"; return; }

    if in_merge_state; then
        err "$proj" "仓库正处于未完成的合并/冲突状态，请先手动处理，已跳过。"
        add_summary "$proj" "error" "仓库已有未完成的合并，先手动处理"
        return
    fi

    # 记录出发点（分支名；detached HEAD 时记 commit）
    local orig_branch
    orig_branch="$(git branch --show-current)"
    if [ -z "$orig_branch" ]; then
        orig_branch="$(git rev-parse --short HEAD)"
        warn "$proj" "当前处于 detached HEAD（${orig_branch}），完成后将切回该提交。"
    fi

    # stash 未提交改动（含未跟踪文件）
    local stashed=0
    if [ -n "$(git status --porcelain)" ]; then
        info "$proj" "检测到未提交改动，stash 保存..."
        if ! git stash push -u -m "$STASH_TAG: $orig_branch" >/dev/null; then
            err "$proj" "stash 失败，已跳过该项目。"
            add_summary "$proj" "error" "stash 失败"
            return
        fi
        stashed=1
    fi

    # 恢复出发点（切回原分支 + 弹出 stash）
    restore_origin() {
        local cur
        cur="$(git branch --show-current)"
        if [ "$cur" != "$orig_branch" ]; then
            if ! git checkout -q "$orig_branch" 2>/dev/null; then
                err "$proj" "切回原分支 $orig_branch 失败！stash 未恢复（如有）。"
                return 1
            fi
        fi
        if [ "$stashed" -eq 1 ]; then
            if git stash pop >/dev/null 2>&1; then
                info "$proj" "已切回 $orig_branch 并恢复 stash 改动。"
            else
                err "$proj" "stash pop 出现冲突或失败，改动保留在 stash 中，请手动执行: git stash pop"
                return 2
            fi
        else
            info "$proj" "已切回 ${orig_branch}。"
        fi
        return 0
    }

    info "$proj" "git fetch origin ..."
    if ! git fetch origin --prune >/dev/null 2>&1; then
        err "$proj" "fetch 失败（检查网络/权限），已跳过。"
        restore_origin
        add_summary "$proj" "error" "git fetch 失败"
        return
    fi

    # 识别主分支
    local main_branch
    if ! main_branch="$(detect_main_branch)"; then
        err "$proj" "无法识别主分支（origin/HEAD、origin/master、origin/main 均不存在），已跳过。"
        restore_origin
        add_summary "$proj" "error" "无法识别主分支"
        return
    fi
    info "$proj" "主分支识别为: $main_branch"

    # 切换到目标分支
    if git show-ref --verify --quiet "refs/heads/$target"; then
        if ! git checkout -q "$target"; then
            err "$proj" "切换到 $target 失败，已跳过。"
            restore_origin
            add_summary "$proj" "error" "切换分支失败"
            return
        fi
    elif git show-ref --verify --quiet "refs/remotes/origin/$target"; then
        info "$proj" "本地没有 ${target}，从 origin/$target 创建..."
        if ! git checkout -q -b "$target" "origin/$target"; then
            err "$proj" "创建分支 $target 失败，已跳过。"
            restore_origin
            add_summary "$proj" "error" "创建分支失败"
            return
        fi
    else
        err "$proj" "分支 $target 在本地和远程都不存在，已跳过。"
        restore_origin
        add_summary "$proj" "error" "分支不存在: $target"
        return
    fi

    # 拉取目标分支最新（fetch 已完成，直接 merge origin/<target>）
    if git show-ref --verify --quiet "refs/remotes/origin/$target"; then
        info "$proj" "合并 origin/$target 最新代码..."
        if ! git merge --no-edit "origin/$target" >/dev/null 2>&1; then
            if in_merge_state; then
                warn "$proj" "${C_BOLD}本地 $target 与远程有冲突！${C_RESET}已停在 $target 分支等待手动处理。"
                add_summary "$proj" "conflict" "本地 $target 与 origin/$target 冲突，需手动解决"
                print_conflict_help "$proj" "$target" "$orig_branch" "$stashed"
                return
            fi
            err "$proj" "合并 origin/$target 失败，已跳过。"
            restore_origin
            add_summary "$proj" "error" "合并 origin/$target 失败"
            return
        fi
    else
        warn "$proj" "origin 上没有 ${target}（本地新分支？），跳过拉取远程目标分支这一步。"
    fi

    # 合并主分支
    info "$proj" "合并 origin/$main_branch -> $target ..."
    local before_merge
    before_merge="$(git rev-parse HEAD)"
    if ! git merge --no-edit "origin/$main_branch" >/dev/null 2>&1; then
        if in_merge_state; then
            warn "$proj" "${C_BOLD}合并主分支有冲突！${C_RESET}已停在 $target 分支等待手动处理。"
            add_summary "$proj" "conflict" "合并 origin/$main_branch 有冲突，需手动解决"
            print_conflict_help "$proj" "$target" "$orig_branch" "$stashed"
            return
        fi
        err "$proj" "合并 origin/$main_branch 失败，已跳过。"
        restore_origin
        add_summary "$proj" "error" "合并主分支失败"
        return
    fi

    # 推送
    local result_msg="已合并主分支并推送"
    if [ "$(git rev-parse HEAD)" = "$before_merge" ] && \
       [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$target" 2>/dev/null || echo none)" ]; then
        ok "$proj" "$target 已包含主分支最新代码，无需推送。"
        result_msg="已是最新，无需推送"
    else
        info "$proj" "推送 $target 到远程..."
        if ! git push origin "$target" >/dev/null 2>&1; then
            err "$proj" "push 失败！合并已完成但未推送，请手动执行: git push origin $target"
            restore_origin
            add_summary "$proj" "error" "push 失败（合并已完成未推送）"
            return
        fi
        ok "$proj" "已推送 $target 到远程。"
    fi

    # 恢复出发点
    restore_origin
    local rc=$?
    if [ "$rc" -eq 2 ]; then
        add_summary "$proj" "conflict" "同步完成，但 stash pop 冲突，需手动恢复改动"
    elif [ "$rc" -ne 0 ]; then
        add_summary "$proj" "error" "同步完成，但切回原分支失败"
    else
        add_summary "$proj" "ok" "${result_msg}，已切回 $orig_branch"
    fi
}

print_conflict_help() {
    local proj="$1" target="$2" orig_branch="$3" stashed="$4"
    log_line help "$proj" "解决冲突后请依次执行："
    log_line help "$proj" "  cd $WWW_DIR/$proj"
    log_line help "$proj" "  # 编辑冲突文件 -> git add <文件> -> git commit"
    log_line help "$proj" "  git push origin $target"
    log_line help "$proj" "  git checkout $orig_branch"
    if [ "$stashed" -eq 1 ]; then
        log_line help "$proj" "  git stash pop    # 恢复你之前的改动"
    fi
}

# ---------- 执行 ----------
for entry in "${ENTRIES[@]}"; do
    proj="${entry%%:*}"
    target="${entry#*:}"
    proj="$(printf '%s' "$proj" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    target="$(printf '%s' "$target" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -z "$proj" ] || [ -z "$target" ]; then
        err "输入" "无法解析该行，已跳过: $entry"
        add_summary "$entry" "error" "格式错误"
        continue
    fi
    process_project "$proj" "$target"
done

# ---------- 汇总报告 ----------
echo ""
echo "${C_BOLD}================ 汇总 ================${C_RESET}"
i=0
HAS_BAD=0
while [ "$i" -lt "${#SUMMARY_PROJ[@]}" ]; do
    case "${SUMMARY_STATE[$i]}" in
        ok)       echo "  ${C_GREEN}✅ ${SUMMARY_PROJ[$i]}${C_RESET} — ${SUMMARY_MSG[$i]}" ;;
        conflict) echo "  ${C_YELLOW}⚠️  ${SUMMARY_PROJ[$i]}${C_RESET} — ${SUMMARY_MSG[$i]}"; HAS_BAD=1 ;;
        *)        echo "  ${C_RED}❌ ${SUMMARY_PROJ[$i]}${C_RESET} — ${SUMMARY_MSG[$i]}"; HAS_BAD=1 ;;
    esac
    i=$((i + 1))
done
exit "$HAS_BAD"
