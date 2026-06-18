#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync-branches-ui.py — 批量分支同步的可视化面板

用法:
    cd /Applications/ServBay/www
    ./sync-branches-ui.py          # 默认端口 8799，自动打开浏览器
    ./sync-branches-ui.py 9000     # 指定端口

功能:
  1. 粘贴「项目：分支」列表，点「检测」—— 用 git merge-tree 预演合并，
     不碰任何工作区，直观看到哪些项目可以自动合并、哪些会冲突需要人工处理。
  2. 点「一键同步」—— 调用同目录的 sync-branches.sh 执行真实同步
     （stash -> 切分支 -> 合并主分支 -> 推送 -> 切回 -> 恢复 stash），
     卡片实时显示每个项目的进度和结果。

零依赖，仅用 python3 标准库；只监听 127.0.0.1。
"""
import json
import os
import subprocess
import sys
import threading
import queue
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 默认仓库根目录：环境变量 SYNC_DEFAULT_BASE 优先（打包成桌面应用时由启动器设置），
# 否则用本文件所在目录
WWW_DIR = os.environ.get("SYNC_DEFAULT_BASE") or os.path.dirname(os.path.abspath(__file__))
GUI_MODE = "--gui" in sys.argv[1:]
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8799)
RESUME_FILE = "sync-branches-resume.json"   # 冲突收尾状态，存在 .git 目录下


# ---------------- git 辅助 ----------------

def run_git(args, cwd, timeout=120):
    """返回 (returncode, stdout+stderr)"""
    try:
        p = subprocess.run(
            ["git"] + args, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时"
    except Exception as e:  # noqa: BLE001
        return 125, str(e)


def parse_entries(text):
    """解析「项目:分支」列表，中英文冒号均可。返回 [(proj, branch), ...]"""
    entries, errors = [], []
    for raw in text.replace("：", ":").splitlines():
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            errors.append(line)
            continue
        proj, _, branch = line.partition(":")
        proj, branch = proj.strip(), branch.strip()
        if proj and branch:
            entries.append((proj, branch))
        else:
            errors.append(line)
    return entries, errors


def parse_project_list(text):
    """解析项目列表：一行一个项目名，忽略空行和注释行。"""
    projects, errors = [], []
    for raw in text.splitlines():
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue
        if any(ch in line for ch in (":", "：")):
            errors.append(line)
            continue
        projects.append(line)
    return projects, errors


def parse_base_dirs(base):
    """解析仓库根目录列表，保持填写顺序。"""
    text = (base or "").strip() or WWW_DIR
    for sep in ("，", "；", ";", ","):
        text = text.replace(sep, "\n")
    dirs = []
    seen = set()
    for raw in text.splitlines():
        path = os.path.abspath(os.path.expanduser(raw.strip()))
        if path and path not in seen:
            seen.add(path)
            dirs.append(path)
    return dirs


def any_base_dir_exists(base):
    return any(os.path.isdir(path) for path in parse_base_dirs(base))


def project_dir(base, proj):
    """按目录顺序查找项目；重名时返回第一个目录里的项目。"""
    dirs = parse_base_dirs(base)
    for root in dirs:
        d = os.path.join(root, proj)
        if os.path.isdir(d):
            return d
    return os.path.join(dirs[0], proj) if dirs else proj


def list_projects(base):
    """列出仓库根目录下一层的 git 项目名。多目录重名时保留第一个。"""
    projects = []
    seen = set()
    for base_dir in parse_base_dirs(base):
        try:
            names = os.listdir(base_dir)
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or name in seen:
                continue
            path = os.path.join(base_dir, name)
            if not os.path.isdir(path):
                continue
            git_marker = os.path.join(path, ".git")
            if os.path.isdir(git_marker) or os.path.isfile(git_marker):
                seen.add(name)
                projects.append(name)
    return sorted(projects, key=lambda s: s.lower())


def detect_main_branch(cwd):
    rc, out = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd)
    if rc == 0 and out.startswith("refs/remotes/origin/"):
        return out[len("refs/remotes/origin/"):]
    for cand in ("master", "main"):
        rc, _ = run_git(["show-ref", "--verify", "--quiet",
                         "refs/remotes/origin/%s" % cand], cwd)
        if rc == 0:
            return cand
    return None


def ref_exists(cwd, ref):
    rc, _ = run_git(["show-ref", "--verify", "--quiet", ref], cwd)
    return rc == 0


def merge_conflicts(cwd, ours, theirs):
    """用 merge-tree 预演 ours+theirs 合并。返回 (是否冲突, 冲突文件列表)"""
    rc, out = run_git(["merge-tree", "--write-tree", "--name-only",
                       ours, theirs], cwd)
    if rc == 0:
        return False, []
    if rc == 1:
        # 输出格式: <tree oid>\n<冲突文件名...>\n<空行>\n<提示信息...>
        files = []
        for l in out.splitlines()[1:]:
            if not l.strip():
                break
            files.append(l.strip())
        return True, files
    return True, ["(merge-tree 异常: %s)" % out[:200]]


def is_ancestor(cwd, ancestor, descendant):
    rc, _ = run_git(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    return rc == 0


def check_one(proj, target, base):
    """无副作用预检一个项目。返回卡片状态 dict。"""
    res = {"proj": proj, "target": target, "logs": [], "conflict_files": []}
    d = project_dir(base, proj)

    def fail(msg):
        res["state"] = "error"
        res["msg"] = msg
        return res

    if not os.path.isdir(d):
        return fail("目录不存在")
    rc, gitdir = run_git(["rev-parse", "--git-dir"], d)
    if rc != 0:
        return fail("不是 git 仓库")
    gitdir = os.path.join(d, gitdir)

    rc, cur = run_git(["branch", "--show-current"], d)
    res["current_branch"] = cur or "(detached)"
    rc, dirty = run_git(["status", "--porcelain"], d)
    res["dirty"] = bool(dirty)

    if os.path.exists(os.path.join(gitdir, "MERGE_HEAD")):
        res["state"] = "conflict"
        res["resume"] = True
        res["msg"] = "仓库处于未完成的合并/冲突状态，解决后可点「收尾」"
        return res

    rc, out = run_git(["fetch", "origin", "--prune"], d, timeout=180)
    if rc != 0:
        return fail("git fetch 失败: %s" % out[:200])

    main = detect_main_branch(d)
    if not main:
        return fail("无法识别主分支")
    res["main"] = main

    local_t = ref_exists(d, "refs/heads/%s" % target)
    remote_t = ref_exists(d, "refs/remotes/origin/%s" % target)
    if not local_t and not remote_t:
        return fail("分支 %s 在本地和远程都不存在" % target)

    conflict_files = []
    # 1) 本地目标分支 vs 远程目标分支
    if local_t and remote_t and not is_ancestor(d, target, "origin/%s" % target) \
            and not is_ancestor(d, "origin/%s" % target, target):
        c, files = merge_conflicts(d, target, "origin/%s" % target)
        if c:
            res["logs"].append("本地 %s 与 origin/%s 合并会冲突" % (target, target))
            conflict_files += files

    # 2) 目标分支 vs 主分支（以推送后的状态为准：远程目标分支优先）
    base_ref = ("origin/%s" % target) if remote_t else target
    up_to_date = is_ancestor(d, "origin/%s" % main, base_ref)
    if local_t and not is_ancestor(d, "origin/%s" % main, target):
        up_to_date = False
    if not up_to_date:
        c, files = merge_conflicts(d, base_ref, "origin/%s" % main)
        if c:
            res["logs"].append("%s 合并 origin/%s 会冲突" % (base_ref, main))
            conflict_files += files
        if local_t and base_ref != target:
            c, files = merge_conflicts(d, target, "origin/%s" % main)
            if c:
                res["logs"].append("本地 %s 合并 origin/%s 会冲突" % (target, main))
                conflict_files += files

    # 目标分支落后主分支多少个提交
    _, behind_s = run_git(["rev-list", "--count",
                           "%s..origin/%s" % (base_ref, main)], d)
    behind = int(behind_s) if behind_s.isdigit() else 0
    res["behind"] = behind
    behind_txt = "，落后主分支 %d 个提交" % behind if behind else ""

    res["conflict_files"] = sorted(set(conflict_files))
    if res["conflict_files"]:
        res["state"] = "conflict"
        res["msg"] = "合并会冲突，需人工处理（%d 个文件%s）" % (
            len(res["conflict_files"]), behind_txt)
    elif up_to_date:
        res["state"] = "uptodate"
        res["msg"] = "已包含主分支最新代码"
    else:
        res["state"] = "clean"
        res["msg"] = "可自动合并推送，无冲突%s" % behind_txt
    return res


# ---------------- SSE 处理 ----------------

def sse_check(entries, write_event, base):
    """并行预检所有项目，完成一个推一个。"""
    q = queue.Queue()

    def worker(p, t):
        try:
            q.put(check_one(p, t, base))
        except Exception as e:  # noqa: BLE001
            q.put({"proj": p, "target": t, "state": "error",
                   "msg": "检测异常: %s" % e, "logs": [], "conflict_files": []})

    threads = [threading.Thread(target=worker, args=e, daemon=True)
               for e in entries]
    for t in threads:
        t.start()
    for _ in entries:
        write_event("check", q.get())
    write_event("done", {})


def create_branch_one(proj, branch, base, push_remote, emit):
    """基于远程主分支创建一个新分支；可选推送远程并设置 upstream。
    若创建前有未提交改动，会 stash 保存但不会恢复到新分支。"""
    def log(level, msg):
        emit("log", {"proj": proj, "level": level, "msg": msg})

    def result(state, msg, **extra):
        emit("result", dict({"proj": proj, "state": state, "msg": msg}, **extra))

    d = project_dir(base, proj)
    if not os.path.isdir(d):
        log("err", "目录不存在: %s" % d)
        return result("error", "目录不存在", stashed=False)
    rc, gitdir = run_git(["rev-parse", "--git-dir"], d)
    if rc != 0:
        log("err", "不是 git 仓库: %s" % d)
        return result("error", "不是 git 仓库", stashed=False)
    gitdir = os.path.join(d, gitdir)

    def blocked_state():
        if os.path.exists(os.path.join(gitdir, "MERGE_HEAD")):
            return "仓库已有未完成的合并，先手动处理"
        if os.path.exists(os.path.join(gitdir, "rebase-merge")) or \
                os.path.exists(os.path.join(gitdir, "rebase-apply")):
            return "仓库已有未完成的 rebase，先手动处理"
        return None

    blocked = blocked_state()
    if blocked:
        log("err", blocked)
        return result("error", blocked, stashed=False)

    rc, out = run_git(["check-ref-format", "--branch", branch], d)
    if rc != 0:
        log("err", "分支名不合法: %s" % branch)
        return result("error", "分支名不合法: %s" % out[:120], stashed=False)

    _, orig = run_git(["branch", "--show-current"], d)
    if not orig:
        _, orig = run_git(["rev-parse", "--short", "HEAD"], d)
        log("warn", "当前处于 detached HEAD（%s）。" % orig)

    stashed = False
    _, dirty = run_git(["status", "--porcelain"], d)
    emit("meta", {"proj": proj, "current_branch": orig, "dirty": bool(dirty)})
    if dirty:
        log("info", "检测到未提交改动，stash 保存到当前分支语义下...")
        rc, out = run_git(["stash", "push", "-u", "-m",
                           "sync-branches-create: %s" % orig], d)
        if rc != 0:
            log("err", "stash 失败，已跳过该项目。")
            return result("error", "stash 失败: %s" % out[:160], stashed=False)
        stashed = True
        log("warn", "旧改动已保存在 stash 中，创建后不会恢复到新分支。")

    log("info", "git fetch origin ...")
    rc, out = run_git(["fetch", "origin", "--prune"], d, timeout=300)
    if rc != 0:
        log("err", "fetch 失败（检查网络/权限），已跳过。")
        return result("error", "git fetch 失败", stashed=stashed)

    main = detect_main_branch(d)
    if not main:
        log("err", "无法识别主分支（origin/HEAD、origin/master、origin/main 均不存在），已跳过。")
        return result("error", "无法识别主分支", stashed=stashed)
    log("info", "主分支识别为: %s" % main)

    if ref_exists(d, "refs/heads/%s" % branch):
        log("info", "本地已存在分支 %s，切换过去..." % branch)
        rc, out = run_git(["checkout", "-q", branch], d)
        if rc != 0:
            log("err", "切换到 %s 失败: %s" % (branch, out[:200]))
            return result("error", "切换本地分支失败", stashed=stashed)
        msg = "本地已存在分支 %s，已切换过去" % branch
        if stashed:
            msg = "%s；旧改动已保存在 stash 中，未恢复到该分支" % msg
        return result("ok", msg, stashed=stashed)
    if ref_exists(d, "refs/remotes/origin/%s" % branch):
        log("info", "远程已存在 origin/%s，拉到本地并切换..." % branch)
        rc, out = run_git(["checkout", "-q", "-b", branch,
                           "origin/%s" % branch], d)
        if rc != 0:
            log("err", "拉取远程分支失败: %s" % out[:200])
            return result("error", "拉取远程分支失败", stashed=stashed)
        msg = "远程已存在 origin/%s，已拉到本地并切换" % branch
        if stashed:
            msg = "%s；旧改动已保存在 stash 中，未恢复到该分支" % msg
        return result("ok", msg, stashed=stashed)

    log("info", "基于 origin/%s 创建 %s ..." % (main, branch))
    rc, out = run_git(["checkout", "-q", "--no-track", "-b", branch,
                       "origin/%s" % main], d)
    if rc != 0:
        log("err", "创建分支失败: %s" % out[:200])
        return result("error", "创建分支失败", stashed=stashed)

    msg = "已基于 origin/%s 创建并切到 %s" % (main, branch)
    if push_remote:
        log("info", "推送 %s 到远程并设置 upstream..." % branch)
        rc, out = run_git(["push", "-u", "origin", branch], d, timeout=300)
        if rc != 0:
            log("err", "push 失败！分支已在本地创建，请手动执行: git push -u origin %s" % branch)
            return result("error", "push 失败（本地分支已创建）",
                          stashed=stashed)
        msg = "%s，已推送远程并设置 upstream" % msg
        log("ok", "已推送 %s 到远程。" % branch)
    else:
        log("ok", "已创建本地分支 %s，未推送远程。" % branch)

    if stashed:
        msg = "%s；旧改动已保存在 stash 中，未恢复到新分支" % msg
    return result("ok", msg, stashed=stashed)


def switch_branch_one(proj, branch, base, emit):
    """切到目标分支，合并远程目标分支和远程主分支最新代码。
    若切换前有未提交改动，会 stash 保存但不会恢复到目标分支。"""
    def log(level, msg):
        emit("log", {"proj": proj, "level": level, "msg": msg})

    def result(state, msg, **extra):
        emit("result", dict({"proj": proj, "state": state, "msg": msg}, **extra))

    d = project_dir(base, proj)
    if not os.path.isdir(d):
        log("err", "目录不存在: %s" % d)
        return result("error", "目录不存在", stashed=False)
    rc, gitdir = run_git(["rev-parse", "--git-dir"], d)
    if rc != 0:
        log("err", "不是 git 仓库: %s" % d)
        return result("error", "不是 git 仓库", stashed=False)
    gitdir = os.path.join(d, gitdir)

    def in_merge_state():
        return os.path.exists(os.path.join(gitdir, "MERGE_HEAD"))

    def in_rebase_state():
        return os.path.exists(os.path.join(gitdir, "rebase-merge")) or \
            os.path.exists(os.path.join(gitdir, "rebase-apply"))

    def save_resume_state():
        try:
            with open(os.path.join(gitdir, RESUME_FILE), "w",
                      encoding="utf-8") as f:
                json.dump({"orig": orig, "target": branch,
                           "stashed": stashed, "mode": "switch"},
                          f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    if in_merge_state():
        log("err", "仓库正处于未完成的合并/冲突状态，请先手动处理，已跳过。")
        return result("error", "仓库已有未完成的合并，先手动处理", stashed=False)
    if in_rebase_state():
        log("err", "仓库正处于未完成的 rebase 状态，请先手动处理，已跳过。")
        return result("error", "仓库已有未完成的 rebase，先手动处理", stashed=False)
    rc, out = run_git(["check-ref-format", "--branch", branch], d)
    if rc != 0:
        log("err", "分支名不合法: %s" % branch)
        return result("error", "分支名不合法: %s" % out[:120], stashed=False)

    _, orig = run_git(["branch", "--show-current"], d)
    if not orig:
        _, orig = run_git(["rev-parse", "--short", "HEAD"], d)
        log("warn", "当前处于 detached HEAD（%s）。" % orig)

    stashed = False
    _, dirty = run_git(["status", "--porcelain"], d)
    emit("meta", {"proj": proj, "current_branch": orig, "dirty": bool(dirty)})
    if dirty:
        log("info", "检测到未提交改动，stash 保存到当前分支语义下...")
        rc, out = run_git(["stash", "push", "-u", "-m",
                           "sync-branches-switch: %s" % orig], d)
        if rc != 0:
            log("err", "stash 失败，已跳过该项目。")
            return result("error", "stash 失败: %s" % out[:160], stashed=False)
        stashed = True
        log("warn", "旧改动已保存在 stash 中，切换后不会恢复到目标分支。")

    log("info", "git fetch origin ...")
    rc, out = run_git(["fetch", "origin", "--prune"], d, timeout=300)
    if rc != 0:
        log("err", "fetch 失败（检查网络/权限），已跳过。")
        return result("error", "git fetch 失败", stashed=stashed)

    main = detect_main_branch(d)
    if not main:
        log("err", "无法识别主分支（origin/HEAD、origin/master、origin/main 均不存在），已跳过。")
        return result("error", "无法识别主分支", stashed=stashed)
    log("info", "主分支识别为: %s" % main)

    if ref_exists(d, "refs/heads/%s" % branch):
        log("info", "切换到本地分支 %s ..." % branch)
        rc, out = run_git(["checkout", "-q", branch], d)
        if rc != 0:
            log("err", "切换到 %s 失败: %s" % (branch, out[:200]))
            return result("error", "切换本地分支失败", stashed=stashed)
    elif ref_exists(d, "refs/remotes/origin/%s" % branch):
        log("info", "本地没有 %s，从 origin/%s 创建并切换..." % (branch, branch))
        rc, out = run_git(["checkout", "-q", "-b", branch,
                           "origin/%s" % branch], d)
        if rc != 0:
            log("err", "拉取远程分支失败: %s" % out[:200])
            return result("error", "拉取远程分支失败", stashed=stashed)
    else:
        log("err", "分支 %s 在本地和远程都不存在，已跳过。" % branch)
        return result("error", "分支不存在: %s" % branch, stashed=stashed)

    if ref_exists(d, "refs/remotes/origin/%s" % branch):
        log("info", "合并 origin/%s 最新代码..." % branch)
        rc, out = run_git(["merge", "--no-edit", "origin/%s" % branch], d)
        if rc != 0:
            if in_merge_state():
                log("warn", "本地 %s 与远程有冲突！已停在 %s 分支等待处理。"
                    % (branch, branch))
                save_resume_state()
                return result("conflict", "本地 %s 与 origin/%s 冲突，需手动解决"
                              % (branch, branch), resume=True, stashed=stashed)
            log("err", "合并 origin/%s 失败: %s" % (branch, out[:200]))
            return result("error", "合并 origin/%s 失败" % branch, stashed=stashed)

    log("info", "合并 origin/%s -> %s ..." % (main, branch))
    rc, out = run_git(["merge", "--no-edit", "origin/%s" % main], d)
    if rc != 0:
        if in_merge_state():
            log("warn", "合并主分支有冲突！已停在 %s 分支等待处理。" % branch)
            save_resume_state()
            return result("conflict", "合并 origin/%s 有冲突，需手动解决" % main,
                          resume=True, stashed=stashed)
        log("err", "合并 origin/%s 失败: %s" % (main, out[:200]))
        return result("error", "合并主分支失败", stashed=stashed)

    msg = "已切换到 %s，并合并远程目标分支和主分支最新代码" % branch
    if stashed:
        msg = "%s；旧改动已保存在 stash 中，未恢复到目标分支" % msg
    log("ok", msg)
    return result("ok", msg, stashed=stashed)


def sync_one(proj, target, base, emit):
    """真实同步一个项目：stash -> 切目标分支 -> 合并远程目标分支 ->
    合并主分支 -> 推送 -> 切回原分支恢复 stash。
    与 sync-branches.sh 同一套流程的跨平台 Python 实现。
    有冲突时保留冲突现场停在目标分支（不切回、不弹 stash）。"""
    def log(level, msg):
        emit("log", {"proj": proj, "level": level, "msg": msg})

    def result(state, msg, **extra):
        emit("result", dict({"proj": proj, "state": state, "msg": msg}, **extra))

    d = project_dir(base, proj)
    if not os.path.isdir(d):
        log("err", "目录不存在: %s" % d)
        return result("error", "目录不存在")
    rc, gitdir = run_git(["rev-parse", "--git-dir"], d)
    if rc != 0:
        log("err", "不是 git 仓库: %s" % d)
        return result("error", "不是 git 仓库")
    gitdir = os.path.join(d, gitdir)  # 相对路径转绝对，绝对路径保持不变

    def in_merge_state():
        return os.path.exists(os.path.join(gitdir, "MERGE_HEAD"))

    def save_resume_state():
        # 留给「冲突收尾」用：记录出发分支和是否 stash 过
        try:
            with open(os.path.join(gitdir, RESUME_FILE), "w",
                      encoding="utf-8") as f:
                json.dump({"orig": orig, "target": target,
                           "stashed": stashed}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    if in_merge_state():
        log("err", "仓库正处于未完成的合并/冲突状态，请先手动处理，已跳过。")
        return result("error", "仓库已有未完成的合并，先手动处理")

    _, orig = run_git(["branch", "--show-current"], d)
    if not orig:
        _, orig = run_git(["rev-parse", "--short", "HEAD"], d)
        log("warn", "当前处于 detached HEAD（%s），完成后将切回该提交。" % orig)

    stashed = False
    _, dirty = run_git(["status", "--porcelain"], d)
    emit("meta", {"proj": proj, "current_branch": orig, "dirty": bool(dirty)})
    if dirty:
        log("info", "检测到未提交改动，stash 保存...")
        rc, _ = run_git(["stash", "push", "-u", "-m",
                         "sync-branches-auto: %s" % orig], d)
        if rc != 0:
            log("err", "stash 失败，已跳过该项目。")
            return result("error", "stash 失败")
        stashed = True

    def restore():
        """切回出发点并恢复 stash。返回 0 成功 / 1 切回失败 / 2 stash pop 失败"""
        _, cur = run_git(["branch", "--show-current"], d)
        if cur != orig:
            rc, _ = run_git(["checkout", "-q", orig], d)
            if rc != 0:
                log("err", "切回原分支 %s 失败！stash 未恢复（如有）。" % orig)
                return 1
        if stashed:
            rc, _ = run_git(["stash", "pop"], d)
            if rc != 0:
                log("err", "stash pop 出现冲突或失败，改动保留在 stash 中，"
                           "请手动执行: git stash pop")
                return 2
            log("info", "已切回 %s 并恢复 stash 改动。" % orig)
        else:
            log("info", "已切回 %s。" % orig)
        return 0

    def conflict_help():
        log("help", "解决冲突后请依次执行：")
        log("help", "  cd %s" % d)
        log("help", "  # 编辑冲突文件 -> git add <文件> -> git commit")
        log("help", "  git push origin %s" % target)
        log("help", "  git checkout %s" % orig)
        if stashed:
            log("help", "  git stash pop    # 恢复你之前的改动")

    log("info", "git fetch origin ...")
    rc, out = run_git(["fetch", "origin", "--prune"], d, timeout=300)
    if rc != 0:
        log("err", "fetch 失败（检查网络/权限），已跳过。")
        restore()
        return result("error", "git fetch 失败")

    main = detect_main_branch(d)
    if not main:
        log("err", "无法识别主分支（origin/HEAD、origin/master、origin/main 均不存在），已跳过。")
        restore()
        return result("error", "无法识别主分支")
    log("info", "主分支识别为: %s" % main)

    if ref_exists(d, "refs/heads/%s" % target):
        rc, _ = run_git(["checkout", "-q", target], d)
        if rc != 0:
            log("err", "切换到 %s 失败，已跳过。" % target)
            restore()
            return result("error", "切换分支失败")
    elif ref_exists(d, "refs/remotes/origin/%s" % target):
        log("info", "本地没有 %s，从 origin/%s 创建..." % (target, target))
        rc, _ = run_git(["checkout", "-q", "-b", target, "origin/%s" % target], d)
        if rc != 0:
            log("err", "创建分支 %s 失败，已跳过。" % target)
            restore()
            return result("error", "创建分支失败")
    else:
        log("err", "分支 %s 在本地和远程都不存在，已跳过。" % target)
        restore()
        return result("error", "分支不存在: %s" % target)

    if ref_exists(d, "refs/remotes/origin/%s" % target):
        log("info", "合并 origin/%s 最新代码..." % target)
        rc, _ = run_git(["merge", "--no-edit", "origin/%s" % target], d)
        if rc != 0:
            if in_merge_state():
                log("warn", "本地 %s 与远程有冲突！已停在 %s 分支等待手动处理。"
                    % (target, target))
                save_resume_state()
                result("conflict", "本地 %s 与 origin/%s 冲突，需手动解决"
                       % (target, target), resume=True)
                return conflict_help()
            log("err", "合并 origin/%s 失败，已跳过。" % target)
            restore()
            return result("error", "合并 origin/%s 失败" % target)
    else:
        log("warn", "origin 上没有 %s（本地新分支？），跳过拉取远程目标分支这一步。" % target)

    _, before = run_git(["rev-parse", "HEAD"], d)
    log("info", "合并 origin/%s -> %s ..." % (main, target))
    rc, _ = run_git(["merge", "--no-edit", "origin/%s" % main], d)
    if rc != 0:
        if in_merge_state():
            log("warn", "合并主分支有冲突！已停在 %s 分支等待手动处理。" % target)
            save_resume_state()
            result("conflict", "合并 origin/%s 有冲突，需手动解决" % main,
                   resume=True)
            return conflict_help()
        log("err", "合并 origin/%s 失败，已跳过。" % main)
        restore()
        return result("error", "合并主分支失败")

    _, head = run_git(["rev-parse", "HEAD"], d)
    rrc, remote_head = run_git(["rev-parse", "origin/%s" % target], d)
    msg = "已合并主分支并推送"
    if head == before and rrc == 0 and head == remote_head:
        log("ok", "%s 已包含主分支最新代码，无需推送。" % target)
        msg = "已是最新，无需推送"
    else:
        log("info", "推送 %s 到远程..." % target)
        rc, out = run_git(["push", "origin", target], d, timeout=300)
        if rc != 0:
            log("err", "push 失败！合并已完成但未推送，请手动执行: git push origin %s" % target)
            restore()
            return result("error", "push 失败（合并已完成未推送）")
        log("ok", "已推送 %s 到远程。" % target)

    code = restore()
    if code == 2:
        result("conflict", "同步完成，但 stash pop 冲突，需手动恢复改动")
    elif code != 0:
        result("error", "同步完成，但切回原分支失败")
    else:
        result("ok", "%s，已切回 %s" % (msg, orig))


def resume_one(proj, target, base, emit):
    """冲突收尾：用户手动解决冲突后，自动 提交合并 -> push -> 切回原分支 -> 恢复 stash。
    出发分支等信息来自 sync_one 冲突时写入 .git 的状态文件。"""
    def log(level, msg):
        emit("log", {"proj": proj, "level": level, "msg": msg})

    def result(state, msg, **extra):
        emit("result", dict({"proj": proj, "state": state, "msg": msg}, **extra))

    d = project_dir(base, proj)
    rc, gitdir = run_git(["rev-parse", "--git-dir"], d) if os.path.isdir(d) else (1, "")
    if rc != 0:
        return result("error", "目录不存在或不是 git 仓库")
    gitdir = os.path.join(d, gitdir)
    state_path = os.path.join(gitdir, RESUME_FILE)
    state = {}
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    orig, stashed = state.get("orig"), state.get("stashed")
    mode = state.get("mode") or "sync"

    _, cur = run_git(["branch", "--show-current"], d)
    if cur != target:
        return result("error", "当前在 %s 分支而不是 %s，无法收尾" % (cur or "?", target))

    rc, unresolved = run_git(["diff", "--name-only", "--diff-filter=U"], d)
    if unresolved:
        return result("conflict", "还有未解决的冲突文件: %s"
                      % " ".join(unresolved.splitlines()[:5]), resume=True)

    if os.path.exists(os.path.join(gitdir, "MERGE_HEAD")):
        log("info", "提交合并结果...")
        rc, out = run_git(["commit", "--no-edit"], d)
        if rc != 0:
            return result("error", "提交合并失败: %s" % out[:200], resume=True)
        log("ok", "合并已提交。")
    else:
        log("info", "合并已提交过，继续收尾。")

    if mode == "switch":
        try:
            os.remove(state_path)
        except OSError:
            pass
        msg = "切换分支冲突已收尾：已提交合并结果，保持在 %s" % target
        if stashed:
            msg = "%s；原分支改动仍保存在 stash 中" % msg
        return result("ok", msg)

    log("info", "推送 %s 到远程..." % target)
    rc, out = run_git(["push", "origin", target], d, timeout=300)
    if rc != 0:
        return result("error", "push 失败: %s" % out[:200], resume=True)
    log("ok", "已推送 %s 到远程。" % target)
    try:
        os.remove(state_path)
    except OSError:
        pass

    if not orig:
        return result("ok", "已推送；没找到出发分支记录，请自行切回原分支")
    rc, _ = run_git(["checkout", "-q", orig], d)
    if rc != 0:
        return result("error", "已推送，但切回 %s 失败" % orig)
    if stashed:
        rc, out = run_git(["stash", "list", "--format=%gd %gs"], d)
        ref = next((ln.split()[0] for ln in out.splitlines()
                    if "sync-branches-auto: %s" % orig in ln), None)
        if ref:
            rc, _ = run_git(["stash", "pop", ref], d)
            if rc != 0:
                return result("conflict",
                              "已推送并切回 %s，但 stash pop 冲突，请手动执行: git stash pop" % orig)
            log("info", "已恢复 stash 改动。")
    result("ok", "冲突已收尾：推送完成，已切回 %s" % orig)


def sse_sync(entries, write_event, base):
    """所有项目并行同步（不同仓库互不影响），事件实时推送。"""
    lock = threading.Lock()

    def emit(name, data):
        with lock:
            write_event(name, data)

    seen, uniq = set(), []
    for p, t in entries:
        if p in seen:
            emit("result", {"proj": p, "state": "error",
                            "msg": "列表里重复出现，已跳过"})
        else:
            seen.add(p)
            uniq.append((p, t))

    def worker(p, t):
        try:
            sync_one(p, t, base, emit)
        except Exception as e:  # noqa: BLE001
            emit("result", {"proj": p, "state": "error", "msg": "同步异常: %s" % e})

    threads = [threading.Thread(target=worker, args=e, daemon=True) for e in uniq]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    write_event("done", {})


def sse_create_branch(projects, branch, push_remote, write_event, base):
    """所有项目并行创建新分支（不同仓库互不影响），事件实时推送。"""
    lock = threading.Lock()

    def emit(name, data):
        with lock:
            write_event(name, data)

    seen, uniq = set(), []
    for p in projects:
        if p in seen:
            emit("result", {"proj": p, "state": "error",
                            "msg": "列表里重复出现，已跳过"})
        else:
            seen.add(p)
            uniq.append(p)

    def worker(p):
        try:
            create_branch_one(p, branch, base, push_remote, emit)
        except Exception as e:  # noqa: BLE001
            emit("result", {"proj": p, "state": "error",
                            "msg": "创建分支异常: %s" % e})

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in uniq]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    write_event("done", {})


def sse_switch_branch(projects, branch, write_event, base):
    """所有项目并行切换分支（不同仓库互不影响），事件实时推送。"""
    lock = threading.Lock()

    def emit(name, data):
        with lock:
            write_event(name, data)

    seen, uniq = set(), []
    for p in projects:
        if p in seen:
            emit("result", {"proj": p, "state": "error",
                            "msg": "列表里重复出现，已跳过"})
        else:
            seen.add(p)
            uniq.append(p)

    def worker(p):
        try:
            switch_branch_one(p, branch, base, emit)
        except Exception as e:  # noqa: BLE001
            emit("result", {"proj": p, "state": "error",
                            "msg": "切换分支异常: %s" % e})

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in uniq]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    write_event("done", {})


def list_stashes(base):
    """扫描根目录下所有 git 仓库，列出本工具创建的 stash。纯本地操作，很快。"""
    found = []
    seen = set()
    for base_dir in parse_base_dirs(base):
        try:
            names = sorted(os.listdir(base_dir))
        except OSError:
            continue
        for name in names:
            if name in seen:
                continue
            d = os.path.join(base_dir, name)
            if not os.path.exists(os.path.join(d, ".git")):
                continue
            seen.add(name)
            rc, txt = run_git(["stash", "list", "--format=%gd|%ci|%gs"], d)
            if rc != 0:
                continue
            for ln in txt.splitlines():
                parts = ln.split("|", 2)
                tags = ("sync-branches-auto:", "sync-branches-create:",
                        "sync-branches-switch:")
                if len(parts) == 3 and any(tag in parts[2] for tag in tags):
                    ref, date, msg = parts
                    tag = next(tag for tag in tags if tag in msg)
                    orig = msg.split(tag, 1)[-1].strip()
                    _, cur = run_git(["branch", "--show-current"], d)
                    found.append({"proj": name, "ref": ref, "date": date[:16],
                                  "branch": orig, "current": cur})
    return found


def stash_detail(base, proj, ref):
    """查看一条 stash 里包含的文件。只在用户点详情时调用。"""
    d = project_dir(base, proj)
    rc, txt = run_git(["stash", "list", "--format=%gd"], d)
    if rc != 0:
        return {"ok": False, "msg": "不是 git 仓库", "files": []}
    refs = set(txt.splitlines())
    if ref not in refs:
        return {"ok": False, "msg": "stash %s 不存在（可能已被恢复）" % ref, "files": []}

    rc, txt = run_git(["stash", "show", "--include-untracked", "--name-status", ref], d)
    if rc != 0:
        return {"ok": False, "msg": "读取 stash 详情失败: %s" % txt[:150], "files": []}

    files = []
    for ln in txt.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[1] if len(parts) == 2 else "%s → %s" % (parts[1], parts[-1])
        files.append({"status": status, "path": path})

    return {"ok": True, "proj": proj, "ref": ref, "files": files}


def pop_stash(base, proj, ref):
    """恢复一条 stash：先切回它所属的分支，再 pop。返回 (ok, msg)。"""
    d = project_dir(base, proj)
    rc, txt = run_git(["stash", "list", "--format=%gd|%gs"], d)
    if rc != 0:
        return False, "不是 git 仓库"
    line = next((l for l in txt.splitlines() if l.startswith(ref + "|")), None)
    if line is None:
        return False, "stash %s 不存在（可能已被恢复）" % ref
    if "sync-branches-auto:" in line:
        orig = line.split("sync-branches-auto:", 1)[-1].strip()
    elif "sync-branches-create:" in line:
        orig = line.split("sync-branches-create:", 1)[-1].strip()
    elif "sync-branches-switch:" in line:
        orig = line.split("sync-branches-switch:", 1)[-1].strip()
    else:
        orig = ""
    _, cur = run_git(["branch", "--show-current"], d)
    if orig and cur != orig:
        rc, out = run_git(["checkout", "-q", orig], d)
        if rc != 0:
            return False, "切回 %s 失败（当前 %s 可能有未提交改动）: %s" % (
                orig, cur, out[:150])
    rc, out = run_git(["stash", "pop", ref], d)
    if rc != 0:
        return False, "stash pop 失败（可能有冲突，改动仍在 stash 中）: %s" % out[:150]
    return True, "已恢复到 %s 分支" % (orig or cur)


# ---------------- 冲突解决 ----------------

def repo_file_path(base, proj, file):
    """拼接并校验文件路径必须落在项目目录内，防止越界。"""
    d = os.path.realpath(project_dir(base, proj))
    p = os.path.realpath(os.path.join(d, file))
    if p != d and not p.startswith(d + os.sep):
        return None, None
    return d, p


def conflict_files(base, proj):
    d = project_dir(base, proj)
    rc, out = run_git(["diff", "--name-only", "--diff-filter=U"], d)
    if rc != 0:
        return None
    return [f for f in out.splitlines() if f.strip()]


def parse_conflict_file(path):
    """把带冲突标记的文件解析成 文本段/冲突段 列表。二进制文件返回 None。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    eol = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith("\n")
    lines = text.splitlines()
    segments, buf = [], []
    i, n = 0, len(lines)

    def is_marker(ln, mark):
        return ln == mark or ln.startswith(mark + " ")

    while i < n:
        ln = lines[i]
        if is_marker(ln, "<<<<<<<"):
            if buf:
                segments.append({"type": "text", "lines": buf})
                buf = []
            ours_label = ln[8:].strip() or "HEAD"
            ours, theirs, theirs_label = [], [], ""
            i += 1
            while i < n and not is_marker(lines[i], "=======") \
                    and not is_marker(lines[i], "|||||||"):
                ours.append(lines[i])
                i += 1
            if i < n and is_marker(lines[i], "|||||||"):   # diff3 风格的 base 段，跳过
                i += 1
                while i < n and not is_marker(lines[i], "======="):
                    i += 1
            if i < n:
                i += 1  # 跳过 =======
            while i < n and not is_marker(lines[i], ">>>>>>>"):
                theirs.append(lines[i])
                i += 1
            if i < n:
                theirs_label = lines[i][8:].strip()
                i += 1
            segments.append({"type": "conflict", "ours": ours, "theirs": theirs,
                             "ours_label": ours_label,
                             "theirs_label": theirs_label or "主分支"})
        else:
            buf.append(ln)
            i += 1
    if buf:
        segments.append({"type": "text", "lines": buf})
    return {"segments": segments, "eol": eol, "trailing": trailing}


def save_resolution(base, proj, file, content):
    """写入解决后的内容并 git add。返回 (ok, msg, 剩余冲突文件数)。"""
    d, p = repo_file_path(base, proj, file)
    if not p:
        return False, "非法文件路径", -1
    try:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except OSError as e:
        return False, "写入失败: %s" % e, -1
    rc, out = run_git(["add", "--", file], d)
    if rc != 0:
        return False, "git add 失败: %s" % out[:150], -1
    remaining = conflict_files(base, proj) or []
    return True, "已解决", len(remaining)


def resolve_file_side(base, proj, file, side):
    """整个文件选一边：side = ours(目标分支) / theirs(主分支)。"""
    d, p = repo_file_path(base, proj, file)
    if not p or side not in ("ours", "theirs"):
        return False, "参数不合法", -1
    rc, out = run_git(["checkout", "--%s" % side, "--", file], d)
    if rc != 0:
        return False, "checkout --%s 失败: %s" % (side, out[:150]), -1
    rc, out = run_git(["add", "--", file], d)
    if rc != 0:
        return False, "git add 失败: %s" % out[:150], -1
    remaining = conflict_files(base, proj) or []
    return True, "已采用%s版本" % ("目标分支" if side == "ours" else "主分支"), len(remaining)


def open_in_editor(base, proj, file):
    d, p = repo_file_path(base, proj, file)
    if not p or not os.path.exists(p):
        return False
    try:
        import shutil
        code = shutil.which("code")
        if code:
            subprocess.Popen([code, "--goto", p])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        elif os.name == "nt":
            os.startfile(p)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except Exception:  # noqa: BLE001
        return False


def sse_resume(proj, target, write_event, base):
    try:
        resume_one(proj, target, base, write_event)
    except Exception as e:  # noqa: BLE001
        write_event("result", {"proj": proj, "state": "error",
                               "msg": "收尾异常: %s" % e})
    write_event("done", {})


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 安静一点
        pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _event(self, name, data):
        payload = "event: %s\ndata: %s\n\n" % (
            name, json.dumps(data, ensure_ascii=False))
        try:
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/sync", "/create", "/switch"):
            page_mode = "sync"
            if path == "/create":
                page_mode = "create"
            elif path == "/switch":
                page_mode = "switch"
            body = PAGE.replace("__DEFAULT_BASE__", json.dumps(WWW_DIR)) \
                       .replace("__PAGE_MODE__", page_mode) \
                       .encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/api/check", "/api/sync", "/api/resume",
                             "/api/create_branch", "/api/switch_branch",
                             "/api/projects", "/api/stashes", "/api/stash_detail", "/api/stash_pop",
                             "/api/conflicts", "/api/conflict_detail",
                             "/api/conflict_save", "/api/conflict_side",
                             "/api/open_editor"):
            self.send_error(404)
            return
        try:
            data = self._json_body()
        except Exception:  # noqa: BLE001
            self.send_error(400)
            return
        base = (data.get("base") or "").strip() or WWW_DIR
        proj = (data.get("proj") or "").strip()
        file = (data.get("file") or "").strip()
        if self.path == "/api/projects":
            self._send_json({"projects": list_projects(base)})
            return
        if self.path == "/api/stashes":
            self._send_json({"stashes": list_stashes(base) if any_base_dir_exists(base) else []})
            return
        if self.path == "/api/stash_detail":
            self._send_json(stash_detail(base, proj, (data.get("ref") or "").strip()))
            return
        if self.path == "/api/stash_pop":
            ok, msg = pop_stash(base, proj, (data.get("ref") or "").strip())
            self._send_json({"ok": ok, "msg": msg})
            return
        if self.path == "/api/conflicts":
            files = conflict_files(base, proj)
            self._send_json({"files": files if files is not None else [],
                             "ok": files is not None})
            return
        if self.path == "/api/conflict_detail":
            _, p = repo_file_path(base, proj, file)
            parsed = parse_conflict_file(p) if p else None
            self._send_json(parsed if parsed else {"binary": True})
            return
        if self.path == "/api/conflict_save":
            ok, msg, remaining = save_resolution(base, proj, file,
                                                 data.get("content") or "")
            self._send_json({"ok": ok, "msg": msg, "remaining": remaining})
            return
        if self.path == "/api/conflict_side":
            ok, msg, remaining = resolve_file_side(base, proj, file,
                                                   (data.get("side") or "").strip())
            self._send_json({"ok": ok, "msg": msg, "remaining": remaining})
            return
        if self.path == "/api/open_editor":
            self._send_json({"ok": open_in_editor(base, proj, file)})
            return
        self._start_sse()
        if not any_base_dir_exists(base):
            self._event("fatal", {"msg": "仓库根目录不存在: %s" % base})
            self._event("done", {})
            return
        if self.path == "/api/resume":
            proj = (data.get("proj") or "").strip()
            target = (data.get("target") or "").strip()
            if proj and target:
                sse_resume(proj, target, self._event, base)
            else:
                self._event("done", {})
            return
        if self.path == "/api/create_branch":
            branch = (data.get("branch") or "").strip()
            if not branch:
                self._event("fatal", {"msg": "新分支名不能为空"})
                self._event("done", {})
                return
            projects, errors = parse_project_list(data.get("projects", ""))
            for bad in errors:
                self._event("parse_error", {"line": bad})
            if not projects:
                self._event("done", {})
                return
            self._event("entries", {"entries": [
                {"proj": p, "target": branch} for p in projects]})
            sse_create_branch(projects, branch, bool(data.get("push")),
                              self._event, base)
            return
        if self.path == "/api/switch_branch":
            branch = (data.get("branch") or "").strip()
            if not branch:
                self._event("fatal", {"msg": "目标分支名不能为空"})
                self._event("done", {})
                return
            projects, errors = parse_project_list(data.get("projects", ""))
            for bad in errors:
                self._event("parse_error", {"line": bad})
            if not projects:
                self._event("done", {})
                return
            self._event("entries", {"entries": [
                {"proj": p, "target": branch} for p in projects]})
            sse_switch_branch(projects, branch, self._event, base)
            return
        entries, errors = parse_entries(data.get("list", ""))
        for bad in errors:
            self._event("parse_error", {"line": bad})
        if not entries:
            self._event("done", {})
            return
        self._event("entries", {"entries": [
            {"proj": p, "target": t} for p, t in entries]})
        if self.path == "/api/check":
            sse_check(entries, self._event, base)
        else:
            sse_sync(entries, self._event, base)


# ---------------- 页面 ----------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>分支同步面板</title>
<style>
  :root {
    --bg:#f5f6f8; --card:#fff; --text:#1f2329; --muted:#8a919f;
    --green:#28a745; --yellow:#e6a23c; --red:#e54d42; --blue:#3370ff; --gray:#9aa4b2;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:24px 20px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  h2 { font-size:16px; margin:0 0 8px; }
  .sub { color:var(--muted); margin-bottom:16px; }
  .topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:14px; }
  .topbar h1 { margin:0; }
  .nav { display:flex; gap:6px; background:#e9edf2; padding:4px; border-radius:8px; }
  .nav a { color:#5b6b87; text-decoration:none; padding:5px 12px; border-radius:6px; font-weight:600; font-size:13px; }
  body[data-page="sync"] .nav-sync,
  body[data-page="create"] .nav-create,
  body[data-page="switch"] .nav-switch { background:var(--card); color:var(--text); box-shadow:0 1px 2px rgba(0,0,0,.08); }
  .page { display:none; }
  body[data-page="sync"] #syncPage,
  body[data-page="create"] #createPage,
  body[data-page="switch"] #switchPage { display:block; }
  textarea { width:100%; height:110px; padding:10px 12px; border:1px solid #d8dce3;
             border-radius:8px; font:13px/1.7 ui-monospace,Menlo,monospace; resize:vertical;
             background:var(--card); }
  textarea:focus { outline:none; border-color:var(--blue); }
  textarea::placeholder, input::placeholder { color:#c5cbd6; opacity:1; }
  .baserow { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
  .baserow label { color:var(--muted); font-size:13px; white-space:nowrap; }
  .baserow input { flex:1; padding:8px 12px; border:1px solid #d8dce3; border-radius:8px;
                   font:13px ui-monospace,Menlo,monospace; background:var(--card); }
  .baserow input:focus { outline:none; border-color:var(--blue); }
  .btns { display:flex; gap:10px; margin:12px 0 20px; align-items:center; }
  button { padding:8px 22px; border:none; border-radius:8px; font-size:14px;
           cursor:pointer; font-weight:600; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  #btnCheck { background:var(--blue); color:#fff; }
  #btnSync  { background:var(--green); color:#fff; }
  #btnSyncClean { background:#1fa860; color:#fff; }
  #btnCreate { background:#7c5cff; color:#fff; }
  #btnSwitch { background:#0f8b8d; color:#fff; }
  #btnCopy { background:#5b6b87; color:#fff; }
  .create-panel { margin:0 0 18px; }
  .create-grid { display:grid; grid-template-columns:1fr 280px; gap:12px; align-items:start; }
  .create-side input { width:100%; padding:8px 12px; border:1px solid #d8dce3; border-radius:8px;
                       font:13px ui-monospace,Menlo,monospace; background:var(--card); }
  .create-side input:focus { outline:none; border-color:var(--blue); }
  .checkline { display:flex; align-items:center; gap:8px; margin-top:10px; color:var(--muted); font-size:13px; }
  .checkline input { width:auto; }
  .project-tools { display:flex; align-items:center; gap:8px; margin-top:8px; }
  .project-tools button, .picker-actions button { padding:5px 14px; font-size:12px;
              background:#e9edf2; color:var(--text); border:1px solid #d8dce3; }
  @media (max-width:760px) { .create-grid { grid-template-columns:1fr; } }
  .stashrow { display:flex; align-items:center; gap:12px; padding:8px 10px;
              border-bottom:1px solid #eef0f3; font-size:13px; }
  .stashrow button { padding:4px 14px; font-size:12px; background:var(--blue); color:#fff; }
  .stashrow button.secondary { background:#5b6b87; }
  .stashfiles { margin:10px 0 0; border:1px solid #eef0f3; border-radius:8px; overflow:hidden; }
  .stashfile { display:grid; grid-template-columns:70px 1fr; gap:12px; padding:8px 10px;
               border-bottom:1px solid #eef0f3; font-family:Menlo,Consolas,monospace; font-size:12px; }
  .stashfile:last-child { border-bottom:none; }
  .stashfile b { color:var(--blue); }
  .pickbox { background:var(--card); max-width:720px; margin:7vh auto; padding:18px 22px;
             border-radius:12px; max-height:76vh; display:flex; flex-direction:column; }
  .picker-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
  .picker-head h3 { margin:0; flex:1; }
  .picker-search { width:100%; padding:8px 12px; border:1px solid #d8dce3; border-radius:8px;
                   font:13px ui-monospace,Menlo,monospace; background:var(--card); }
  .picker-actions { display:flex; align-items:center; gap:8px; margin:10px 0; }
  .picker-list { overflow:auto; border:1px solid #eef0f3; border-radius:8px; min-height:180px; }
  .pickrow { display:flex; align-items:center; gap:8px; padding:7px 10px; border-bottom:1px solid #eef0f3;
             font:13px ui-monospace,Menlo,monospace; cursor:pointer; }
  .pickrow:hover { background:#f5f7fa; }
  .pickrow:last-child { border-bottom:none; }
  .pickrow input { width:auto; }
  .picker-foot { display:flex; align-items:center; gap:8px; justify-content:flex-end; margin-top:12px; }
  .picker-foot button { padding:6px 16px; font-size:13px; }
  #projectPickerApply { background:var(--blue); color:#fff; }
  .retry, .fix { display:none; margin-top:8px; margin-right:8px; padding:5px 14px; font-size:12px;
           background:var(--blue); color:#fff; border:none; border-radius:6px;
           cursor:pointer; font-weight:600; }
  .fix { background:var(--yellow); }
  /* ---- 冲突解决器 ---- */
  #resModal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:20; }
  .resbox { background:var(--bg); width:92%; max-width:1080px; height:86vh; margin:5vh auto;
            border-radius:12px; display:flex; flex-direction:column; overflow:hidden; }
  .reshead { display:flex; align-items:center; gap:10px; padding:12px 18px;
             background:var(--card); border-bottom:1px solid #e3e6eb; }
  .reshead b { font-size:15px; }
  .resbody { display:flex; flex:1; min-height:0; }
  .resfiles { width:230px; overflow:auto; border-right:1px solid #e3e6eb;
              background:var(--card); padding:8px 0; }
  .resfile { padding:7px 14px; font:12px ui-monospace,Menlo,monospace; cursor:pointer;
             word-break:break-all; }
  .resfile:hover { background:#f0f3f8; }
  .resfile.active { background:#e6eefc; font-weight:700; }
  .resfile.done { color:var(--green); }
  .resmain { flex:1; overflow:auto; padding:14px 18px; }
  .ctx { margin:0; padding:6px 10px; font:12px/1.6 ui-monospace,Menlo,monospace;
         color:#9aa4b2; white-space:pre-wrap; word-break:break-all; }
  .cblock { border:1px solid #e7c98a; border-radius:8px; margin:10px 0; overflow:hidden; }
  .cbar { display:flex; gap:8px; align-items:center; padding:6px 10px; background:#fdf6e7;
          font-size:12px; }
  .cbar button { padding:3px 12px; font-size:12px; border-radius:5px;
                 background:#fff; border:1px solid #d8dce3; color:var(--text); font-weight:500; }
  .cbar button.on { background:var(--blue); color:#fff; border-color:var(--blue); }
  .cpane { padding:6px 10px; font:12px/1.6 ui-monospace,Menlo,monospace;
           white-space:pre-wrap; word-break:break-all; }
  .cpane.ours { background:#eef4ff; border-top:1px solid #e3e6eb; }
  .cpane.theirs { background:#fff4ec; border-top:1px solid #f0e0d0; }
  .cpane .plabel { font-weight:700; font-size:11px; color:#5b6b87; display:block; }
  .cblock.picked-ours .cpane.theirs, .cblock.picked-theirs .cpane.ours { opacity:.35; }
  .cblock textarea { width:100%; min-height:110px; border:none; padding:8px 10px;
                     font:12px/1.6 ui-monospace,Menlo,monospace; background:#fffef5; }
  .resfoot { display:flex; gap:8px; align-items:center; padding:10px 18px;
             background:var(--card); border-top:1px solid #e3e6eb; }
  .resfoot button { padding:6px 14px; font-size:12px; }
  #resSave { background:var(--green); color:#fff; }
  #resOursAll, #resTheirsAll, #resEditor { background:#e9edf2; color:var(--text); }
  .resdone { text-align:center; padding:60px 20px; }
  .resdone button { background:var(--green); color:#fff; padding:10px 26px; font-size:15px; }
  .hint { color:var(--muted); font-size:12px; }
  .legend { display:flex; gap:16px; margin-bottom:10px; font-size:12px; color:var(--muted); }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:14px; }
  .card { background:var(--card); border-radius:10px; padding:14px 16px;
          border-left:5px solid var(--gray); box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .card.clean    { border-left-color:var(--green); }
  .card.uptodate { border-left-color:var(--gray); }
  .card.ok       { border-left-color:var(--green); }
  .card.exists   { border-left-color:var(--gray); }
  .card.conflict { border-left-color:var(--yellow); background:#fffaf0; }
  .card.error    { border-left-color:var(--red); background:#fff5f5; }
  .card.running  { border-left-color:var(--blue); }
  .card h3 { margin:0; font-size:15px; display:flex; align-items:center; gap:8px; }
  .badge { font-size:11px; padding:1px 8px; border-radius:10px; color:#fff;
           background:var(--gray); white-space:nowrap; }
  .badge.clean,.badge.ok { background:var(--green); }
  .badge.exists  { background:var(--gray); }
  .badge.conflict { background:var(--yellow); }
  .badge.error    { background:var(--red); }
  .badge.running  { background:var(--blue); }
  .target { color:var(--muted); font-size:12px; margin:2px 0 6px; }
  .msg { font-size:13px; }
  .files { margin:6px 0 0; padding:8px 10px; background:#f7f3e8; border-radius:6px;
           font:12px/1.7 ui-monospace,Menlo,monospace; word-break:break-all; }
  .resume { display:none; margin-top:8px; padding:5px 14px; font-size:12px;
            background:var(--yellow); color:#fff; border:none; border-radius:6px;
            cursor:pointer; font-weight:600; }
  details { margin-top:8px; }
  summary { cursor:pointer; color:var(--muted); font-size:12px; }
  .loglines { margin:6px 0 0; padding:8px 10px; background:#f2f4f7; border-radius:6px;
              font:11px/1.7 ui-monospace,Menlo,monospace; max-height:220px; overflow:auto;
              white-space:pre-wrap; word-break:break-all; }
  .loglines .err  { color:var(--red); }
  .loglines .warn { color:#b8860b; }
  .loglines .help { color:var(--blue); }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid #c9d4f5;
          border-top-color:var(--blue); border-radius:50%;
          animation:r .8s linear infinite; }
  @keyframes r { to { transform:rotate(360deg); } }
</style>
</head>
<body data-page="__PAGE_MODE__">
<div class="wrap">
  <div class="topbar">
    <h1>分支同步面板</h1>
    <nav class="nav">
      <a class="nav-sync" href="/sync">同步分支</a>
      <a class="nav-create" href="/create">创建新分支</a>
      <a class="nav-switch" href="/switch">切换分支</a>
    </nav>
  </div>
  <div class="baserow">
    <label for="base">仓库根目录</label>
    <input id="base" type="text" spellcheck="false" autocorrect="off"
           autocapitalize="off" autocomplete="off"
           placeholder="可填多个目录，用逗号、分号或换行分隔">
  </div>
  <section class="page" id="syncPage">
    <div class="sub">粘贴「项目：分支」列表 → <b>检测</b>（不动代码，预演合并）→ <b>一键同步</b>（stash、合并主分支、推送、切回并恢复现场）</div>
    <textarea id="list" spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off"
              placeholder="mix_ads_web：dev_ws_api_product&#10;mix_ads_ws：dev_ws_api_product&#10;rpc_process：dev_ws_api_product"></textarea>
    <div class="btns">
      <button id="btnCheck">检 测</button>
      <button id="btnSync" disabled>一键同步</button>
      <button id="btnSyncClean" disabled>只同步无冲突项</button>
      <button id="btnCopy" style="display:none">复制结果汇总</button>
      <span class="hint" id="status"></span>
    </div>
  </section>
  <section class="page" id="createPage">
    <div class="sub">一行一个项目名，填写一次新分支名，基于远程主分支批量创建并切过去。</div>
    <div class="create-panel">
      <h2>创建新分支</h2>
      <div class="create-grid">
        <div>
          <textarea id="createProjects" spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off"
                    placeholder="mix_ads_web&#10;mix_ads_ws&#10;rpc_process"></textarea>
          <div class="project-tools">
            <button id="btnPickCreate" type="button">选择项目</button>
            <span class="hint">从仓库目录中选择已有 git 项目，重名项目取前面的目录</span>
          </div>
        </div>
        <div class="create-side">
          <input id="createBranch" type="text" spellcheck="false" autocorrect="off"
                 autocapitalize="off" autocomplete="off" placeholder="dev_new_requirement">
          <label class="checkline">
            <input id="createPush" type="checkbox" checked>
            推送到远程并设置 upstream
          </label>
          <div class="btns" style="margin-bottom:0">
            <button id="btnCreate">创建分支</button>
            <button id="btnCopyCreate" style="display:none;background:#5b6b87;color:#fff">复制结果汇总</button>
            <span class="hint" id="createStatus"></span>
          </div>
        </div>
      </div>
    </div>
  </section>
  <section class="page" id="switchPage">
    <div class="sub">一行一个项目名，填写一次目标分支名，批量切到该分支，并合并远程目标分支和远程主分支最新代码。</div>
    <div class="create-panel">
      <h2>切换分支</h2>
      <div class="create-grid">
        <div>
          <textarea id="switchProjects" spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off"
                    placeholder="mix_ads_web&#10;mix_ads_ws&#10;rpc_process"></textarea>
          <div class="project-tools">
            <button id="btnPickSwitch" type="button">选择项目</button>
            <span class="hint">从仓库目录中选择已有 git 项目，重名项目取前面的目录</span>
          </div>
        </div>
        <div class="create-side">
          <input id="switchBranch" type="text" spellcheck="false" autocorrect="off"
                 autocapitalize="off" autocomplete="off" placeholder="dev_requirement">
          <div class="btns" style="margin-bottom:0">
            <button id="btnSwitch">切换分支</button>
            <button id="btnCopySwitch" style="display:none;background:#5b6b87;color:#fff">复制结果汇总</button>
            <span class="hint" id="switchStatus"></span>
          </div>
        </div>
      </div>
    </div>
  </section>
  <div class="legend">
    <span><span class="dot" style="background:var(--green)"></span>可自动合并 / 已完成</span>
    <span><span class="dot" style="background:var(--yellow)"></span>有冲突，需人工处理</span>
    <span><span class="dot" style="background:var(--red)"></span>出错</span>
    <span><span class="dot" style="background:var(--gray)"></span>已是最新</span>
    <a href="javascript:void(0)" id="stashLink" style="margin-left:auto;color:var(--blue)">stash 恢复中心</a>
  </div>
  <div class="cards" id="cards"></div>
</div>
<div id="resModal">
  <div class="resbox">
    <div class="reshead">
      <b id="resTitle"></b>
      <span class="hint" id="resHint" style="flex:1"></span>
      <button id="resClose" style="background:#e3e6eb;color:var(--text)">关 闭</button>
    </div>
    <div class="resbody">
      <div class="resfiles" id="resFiles"></div>
      <div class="resmain" id="resMain"></div>
    </div>
    <div class="resfoot">
      <button id="resOursAll">整个文件用目标分支版本</button>
      <button id="resTheirsAll">整个文件用主分支版本</button>
      <button id="resEditor">用编辑器打开</button>
      <span style="flex:1"></span>
      <button id="resSave" disabled>保存此文件（git add）</button>
    </div>
  </div>
</div>
<div id="projectPickerModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:12">
  <div class="pickbox">
    <div class="picker-head">
      <h3>选择项目</h3>
      <span class="hint" id="projectPickerCount"></span>
      <button id="projectPickerClose" style="background:#e3e6eb;color:var(--text)">关 闭</button>
    </div>
    <input id="projectPickerSearch" class="picker-search" type="text" spellcheck="false"
           autocorrect="off" autocapitalize="off" autocomplete="off" placeholder="搜索项目名">
    <div class="picker-actions">
      <button id="projectPickerAll" type="button">全选当前结果</button>
      <button id="projectPickerClear" type="button">清空选择</button>
      <span class="hint" id="projectPickerHint"></span>
    </div>
    <div id="projectPickerList" class="picker-list"></div>
    <div class="picker-foot">
      <button id="projectPickerCancel" style="background:#e3e6eb;color:var(--text)">取 消</button>
      <button id="projectPickerApply">确认选择</button>
    </div>
  </div>
</div>
<div id="stashModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:10">
  <div style="background:var(--card);max-width:680px;margin:8vh auto;padding:20px 24px;border-radius:12px;max-height:70vh;overflow:auto">
    <h3 style="margin:0 0 4px">stash 恢复中心</h3>
    <div class="hint" style="margin-bottom:10px">工具自动 stash 的改动都在这里。「恢复」会先切回改动所属的分支再取出改动。</div>
    <div id="stashList"></div>
    <div style="text-align:right;margin-top:12px">
      <button id="stashClose" style="background:#e3e6eb;color:var(--text)">关 闭</button>
    </div>
  </div>
</div>
<div id="stashDetailModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:11">
  <div style="background:var(--card);max-width:620px;margin:12vh auto;padding:18px 22px;border-radius:12px;max-height:68vh;overflow:auto">
    <h3 id="stashDetailTitle" style="margin:0 0 4px">stash 详情</h3>
    <div class="hint" id="stashDetailMeta" style="margin-bottom:10px"></div>
    <div id="stashDetailBody"></div>
    <div style="text-align:right;margin-top:12px">
      <button id="stashDetailClose" style="background:#e3e6eb;color:var(--text)">关 闭</button>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const cards = {};
let running = false;
const PAGE_MODE = document.body.dataset.page || 'sync';
const statusBox = () => PAGE_MODE === 'create' ? $('createStatus')
  : PAGE_MODE === 'switch' ? $('switchStatus') : $('status');
const copyButton = () => PAGE_MODE === 'create' ? $('btnCopyCreate')
  : PAGE_MODE === 'switch' ? $('btnCopySwitch') : $('btnCopy');

const DEFAULT_BASE = __DEFAULT_BASE__;
$('list').value = localStorage.getItem('sync_list') || '';
$('list').addEventListener('input', () => localStorage.setItem('sync_list', $('list').value));
$('base').value = localStorage.getItem('sync_base') || DEFAULT_BASE;
$('base').addEventListener('input', () => localStorage.setItem('sync_base', $('base').value));
$('createProjects').value = localStorage.getItem('create_projects') || '';
$('createProjects').addEventListener('input', () => localStorage.setItem('create_projects', $('createProjects').value));
$('createBranch').value = localStorage.getItem('create_branch') || '';
$('createBranch').addEventListener('input', () => localStorage.setItem('create_branch', $('createBranch').value));
$('createPush').checked = localStorage.getItem('create_push') !== '0';
$('createPush').addEventListener('change', () => localStorage.setItem('create_push', $('createPush').checked ? '1' : '0'));
$('switchProjects').value = localStorage.getItem('switch_projects') || '';
$('switchProjects').addEventListener('input', () => localStorage.setItem('switch_projects', $('switchProjects').value));
$('switchBranch').value = localStorage.getItem('switch_branch') || '';
$('switchBranch').addEventListener('input', () => localStorage.setItem('switch_branch', $('switchBranch').value));

let pickerTarget = null;
let pickerProjects = [];
let pickerSelected = new Set();

function projectTextarea(mode) {
  return mode === 'switch' ? $('switchProjects') : $('createProjects');
}

function parseProjectText(text) {
  return text.split(/\r?\n/)
    .map(s => s.trim())
    .filter(s => s && !s.startsWith('#') && !s.includes(':') && !s.includes('：'));
}

async function openProjectPicker(mode) {
  pickerTarget = mode;
  pickerProjects = [];
  pickerSelected = new Set(parseProjectText(projectTextarea(mode).value));
  $('projectPickerModal').style.display = '';
  $('projectPickerSearch').value = '';
  $('projectPickerHint').textContent = '加载中…';
  $('projectPickerList').innerHTML = '<div class="hint" style="padding:12px">加载中…</div>';
  try {
    const r = await fetch('/api/projects', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base: $('base').value})});
    const data = await r.json();
    pickerProjects = data.projects || [];
    $('projectPickerHint').textContent = pickerProjects.length
      ? '已读取 ' + pickerProjects.length + ' 个项目'
      : '当前仓库根目录下没有识别到 git 项目';
    renderProjectPicker();
    $('projectPickerSearch').focus();
  } catch (e) {
    $('projectPickerHint').textContent = '读取项目失败: ' + e;
    $('projectPickerList').innerHTML = '';
  }
}

function visiblePickerProjects() {
  const q = $('projectPickerSearch').value.trim().toLowerCase();
  return pickerProjects.filter(p => !q || p.toLowerCase().includes(q));
}

function renderProjectPicker() {
  const list = visiblePickerProjects();
  $('projectPickerCount').textContent = '已选 ' + pickerSelected.size + ' 个';
  if (!list.length) {
    $('projectPickerList').innerHTML = '<div class="hint" style="padding:12px">没有匹配的项目</div>';
    return;
  }
  $('projectPickerList').innerHTML = '';
  for (const name of list) {
    const row = document.createElement('label');
    row.className = 'pickrow';
    const checked = pickerSelected.has(name) ? ' checked' : '';
    row.innerHTML = `<input type="checkbox"${checked}><span></span>`;
    row.querySelector('span').textContent = name;
    row.querySelector('input').onchange = e => {
      if (e.target.checked) pickerSelected.add(name);
      else pickerSelected.delete(name);
      renderProjectPicker();
    };
    $('projectPickerList').appendChild(row);
  }
}

function closeProjectPicker() {
  $('projectPickerModal').style.display = 'none';
}

$('btnPickCreate').onclick = () => openProjectPicker('create');
$('btnPickSwitch').onclick = () => openProjectPicker('switch');
$('projectPickerClose').onclick = closeProjectPicker;
$('projectPickerCancel').onclick = closeProjectPicker;
$('projectPickerSearch').addEventListener('input', renderProjectPicker);
$('projectPickerAll').onclick = () => {
  visiblePickerProjects().forEach(p => pickerSelected.add(p));
  renderProjectPicker();
};
$('projectPickerClear').onclick = () => {
  pickerSelected.clear();
  renderProjectPicker();
};
$('projectPickerApply').onclick = () => {
  const ordered = pickerProjects.filter(p => pickerSelected.has(p));
  const extra = Array.from(pickerSelected).filter(p => !pickerProjects.includes(p)).sort();
  const target = projectTextarea(pickerTarget || 'create');
  target.value = ordered.concat(extra).join('\n');
  target.dispatchEvent(new Event('input'));
  closeProjectPicker();
};

const STATE_TXT = {
  running:'处理中', clean:'可自动合并', uptodate:'已是最新',
  conflict:'需人工处理', error:'出错', ok:'已完成', exists:'已存在'
};

function makeCard(proj, target, mode) {
  mode = mode || 'sync';
  const targetLabel = mode === 'create' ? '新分支: ' : mode === 'switch' ? '切换到: ' : '目标分支: ';
  let el = cards[proj];
  if (el) {              // 局部同步/重试时复用卡片，重置为处理中
    el.className = 'card running';
    el.dataset.mode = mode;
    el.dataset.target = target;
    el.dataset.state = 'running';
    el.querySelector('.target').textContent = targetLabel + target;
    const b = el.querySelector('.badge');
    b.className = 'badge running';
    b.innerHTML = '<span class="spin"></span>';
    el.querySelector('.msg').textContent = '处理中…';
    el.querySelector('.files').style.display = 'none';
    el.querySelector('.resume').style.display = 'none';
    el.querySelector('.fix').style.display = 'none';
    el.querySelector('.retry').style.display = 'none';
    el.querySelector('.loglines').innerHTML = '';
    return el;
  }
  el = document.createElement('div');
  el.className = 'card running';
  el.dataset.target = target;
  el.dataset.mode = mode;
  el.dataset.state = 'running';
  el.innerHTML = `
    <h3><span class="name"></span><span class="badge running"><span class="spin"></span></span></h3>
    <div class="target"></div>
    <div class="target cur">当前分支: …</div>
    <div class="msg">处理中…</div>
    <div class="files" style="display:none"></div>
    <button class="retry">重 试</button><button class="fix">解决冲突</button><button class="resume">一键收尾</button>
    <details style="display:none"><summary>详细日志</summary><div class="loglines"></div></details>`;
  el.querySelector('.name').textContent = proj;
  el.querySelector('.target').textContent = targetLabel + target;
  el.querySelector('.resume').onclick = () => doResume(proj, target);
  el.querySelector('.fix').onclick = () => openResolver(proj, target);
  el.querySelector('.retry').onclick = () => {
    if (el.dataset.mode === 'create') doCreate(proj, target, $('createPush').checked, true, '重试 ' + proj + ' …');
    else if (el.dataset.mode === 'switch') doSwitch(proj, target, true, '重试 ' + proj + ' …');
    else doSync(proj + ':' + target, true, '重试 ' + proj + ' …');
  };
  $('cards').appendChild(el);
  cards[proj] = el;
  return el;
}

function setCur(proj, branch, dirty) {
  const el = cards[proj]; if (!el) return;
  const dirtyMsg = el.dataset.mode === 'create' || el.dataset.mode === 'switch'
    ? '（有未提交改动，会自动 stash，不会恢复到目标分支）'
    : '（有未提交改动，会自动 stash 并恢复）';
  el.querySelector('.cur').textContent = branch
    ? '当前分支: ' + branch + (dirty ? dirtyMsg : '')
    : '';
}

function setState(proj, state, msg, resume) {
  const el = cards[proj]; if (!el) return;
  el.className = 'card ' + state;
  el.dataset.state = state;
  const b = el.querySelector('.badge');
  b.className = 'badge ' + state;
  b.textContent = STATE_TXT[state] || state;
  el.querySelector('.msg').textContent = msg || '';
  el.querySelector('.resume').style.display = resume ? '' : 'none';
  el.querySelector('.fix').style.display = resume ? '' : 'none';
  el.querySelector('.retry').style.display = state === 'error' ? '' : 'none';
  const cur = el.querySelector('.cur');
  if (state === 'error' && cur.textContent.endsWith('…')) cur.textContent = '';
}

async function doResume(proj, target) {
  if (running) return;
  running = true;
  $('btnCheck').disabled = true; $('btnSync').disabled = true;
  const mode = cards[proj] ? cards[proj].dataset.mode : 'sync';
  const msg = mode === 'switch'
    ? '收尾中：提交合并，保持在目标分支…'
    : '收尾中：提交合并、推送、切回原分支…';
  setState(proj, 'running', msg);
  const b = cards[proj].querySelector('.badge');
  b.innerHTML = '<span class="spin"></span>';
  let failMsg = '';
  try {
    await stream('/api/resume', {proj, target, base: $('base').value}, (ev, d) => {
      if (ev === 'log') addLog(d.proj, d.level, d.msg);
      else if (ev === 'result') setState(d.proj, d.state, d.msg, d.resume);
      else if (ev === 'fatal') failMsg = d.msg;
    });
  } catch (e) {
    failMsg = '收尾连接中断: ' + e;
    setState(proj, 'error', failMsg);
  } finally {
    running = false;
    $('btnCheck').disabled = false;
    $('btnCreate').disabled = false;
    $('btnSwitch').disabled = false;
    $('btnPickCreate').disabled = false;
    $('btnPickSwitch').disabled = false;
    statusBox().textContent = failMsg || '';
  }
}

function addLog(proj, level, msg) {
  const el = cards[proj]; if (!el) return;
  const det = el.querySelector('details');
  det.style.display = '';
  const span = document.createElement('div');
  span.className = level;
  span.textContent = msg;
  el.querySelector('.loglines').appendChild(span);
}

function showFiles(proj, files) {
  const el = cards[proj]; if (!el || !files || !files.length) return;
  const f = el.querySelector('.files');
  f.style.display = '';
  f.textContent = '冲突文件:\n' + files.join('\n');
}

async function stream(url, body, onEvent) {
  const resp = await fetch(url, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream:true});
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      let ev = 'message', data = '';
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event: ')) ev = line.slice(7);
        else if (line.startsWith('data: ')) data = line.slice(6);
      }
      if (data) {
        try { onEvent(ev, JSON.parse(data)); }
        catch (e) { console.error('事件处理失败', ev, data, e); }
      }
    }
  }
}

function begin(label, keepCards) {
  running = true;
  $('btnCheck').disabled = true; $('btnSync').disabled = true;
  $('btnSyncClean').disabled = true;
  $('btnCreate').disabled = true;
  $('btnSwitch').disabled = true;
  $('btnPickCreate').disabled = true;
  $('btnPickSwitch').disabled = true;
  statusBox().textContent = label;
  if (!keepCards) {
    $('cards').innerHTML = '';
    for (const k in cards) delete cards[k];
  }
}
function end(allowSync) {
  running = false;
  $('btnCheck').disabled = false;
  $('btnCreate').disabled = false;
  $('btnSwitch').disabled = false;
  $('btnPickCreate').disabled = false;
  $('btnPickSwitch').disabled = false;
  $('btnSync').disabled = !allowSync;
  $('btnSyncClean').disabled = !(allowSync && cleanProjects().length);
  copyButton().style.display = Object.keys(cards).length ? '' : 'none';
  statusBox().textContent = '';
}
function cleanProjects() {
  return Object.keys(cards).filter(p => cards[p].dataset.state === 'clean');
}

// 流结束后，仍停在「处理中」的卡片标记为未返回结果
function failPending(msg) {
  for (const proj in cards) {
    if (cards[proj].classList.contains('running')) setState(proj, 'error', msg);
  }
}

$('btnCheck').onclick = async () => {
  if (running) return;
  begin('检测中（不会改动任何代码）…');
  let failMsg = '';
  try {
    await stream('/api/check', {list: $('list').value, base: $('base').value}, (ev, d) => {
      if (ev === 'entries') d.entries.forEach(e => makeCard(e.proj, e.target));
      else if (ev === 'fatal') { failMsg = d.msg; }
      else if (ev === 'parse_error') { failMsg = '有无法解析的行: ' + d.line; }
      else if (ev === 'check') {
        setState(d.proj, d.state, d.msg, d.resume);
        setCur(d.proj, d.current_branch, d.dirty);
        (d.logs||[]).forEach(m => addLog(d.proj, 'warn', m));
        showFiles(d.proj, d.conflict_files);
      }
    });
  } catch (e) {
    failMsg = '检测中断: ' + e + '（请确认面板服务还在运行，刷新页面重试）';
  } finally {
    failPending('检测未返回结果');
    end(Object.keys(cards).length > 0);
    statusBox().textContent = failMsg ||
      '检测完成。绿色项可放心一键同步；黄色项同步后会停在冲突现场等你处理。';
  }
};

async function doSync(listText, keepCards, label) {
  if (running) return;
  if (!keepCards &&
      !confirm('确认执行同步？将合并主分支并推送到远程。\n有冲突的项目会停在冲突现场等你手动处理。')) return;
  begin(label || '同步中…', keepCards);
  let failMsg = '';
  const touched = new Set();
  try {
    await stream('/api/sync', {list: listText, base: $('base').value}, (ev, d) => {
      if (ev === 'entries') d.entries.forEach(e => { touched.add(e.proj); makeCard(e.proj, e.target); });
      else if (ev === 'fatal') { failMsg = d.msg; }
      else if (ev === 'meta') setCur(d.proj, d.current_branch, d.dirty);
      else if (ev === 'log') {
        addLog(d.proj, d.level, d.msg);
        const el = cards[d.proj];
        if (el && el.classList.contains('running')) el.querySelector('.msg').textContent = d.msg;
      }
      else if (ev === 'result') setState(d.proj, d.state, d.msg, d.resume);
    });
  } catch (e) {
    failMsg = '同步连接中断: ' + e + '（请到各项目里 git status 确认状态）';
  } finally {
    for (const proj of touched) {
      if (cards[proj] && cards[proj].classList.contains('running'))
        setState(proj, 'error', '未返回结果');
    }
    end(false);
    statusBox().textContent = failMsg ||
      '同步完成。出错项可点卡片上的「重试」，冲突项解决后点「一键收尾」。';
  }
}

$('btnSync').onclick = () => doSync($('list').value, false);

$('btnSyncClean').onclick = () => {
  const list = cleanProjects().map(p => p + ':' + cards[p].dataset.target).join('\n');
  if (list) doSync(list, true, '同步无冲突项…');
};

async function doCreate(projectText, branch, pushRemote, keepCards, label) {
  if (running) return;
  branch = (branch || '').trim();
  if (!branch) {
    statusBox().textContent = '请填写新分支名';
    $('createBranch').focus();
    return;
  }
  const pushText = pushRemote ? '，并推送到远程' : '，不推送远程';
  if (!keepCards &&
      !confirm('确认创建新分支 ' + branch + '？\n会基于远程主分支创建并切过去' + pushText + '。\n当前未提交改动会被 stash，且不会恢复到新分支。')) return;
  begin(label || '创建分支中…', keepCards);
  let failMsg = '';
  const touched = new Set();
  try {
    await stream('/api/create_branch',
      {projects: projectText, branch, push: pushRemote, base: $('base').value},
      (ev, d) => {
        if (ev === 'entries') d.entries.forEach(e => { touched.add(e.proj); makeCard(e.proj, e.target, 'create'); });
        else if (ev === 'fatal') { failMsg = d.msg; }
        else if (ev === 'parse_error') { failMsg = '有无法解析的项目行: ' + d.line; }
        else if (ev === 'meta') setCur(d.proj, d.current_branch, d.dirty);
        else if (ev === 'log') {
          addLog(d.proj, d.level, d.msg);
          const el = cards[d.proj];
          if (el && el.classList.contains('running')) el.querySelector('.msg').textContent = d.msg;
        }
        else if (ev === 'result') setState(d.proj, d.state, d.msg, false);
      });
  } catch (e) {
    failMsg = '创建分支连接中断: ' + e + '（请到各项目里 git status 确认状态）';
  } finally {
    for (const proj of touched) {
      if (cards[proj] && cards[proj].classList.contains('running'))
        setState(proj, 'error', '未返回结果');
    }
    end(false);
    statusBox().textContent = failMsg ||
      '创建分支完成。发生 stash 的项目，旧改动已保存在 stash 恢复中心。';
  }
}

$('btnCreate').onclick = () =>
  doCreate($('createProjects').value, $('createBranch').value, $('createPush').checked, false);

async function doSwitch(projectText, branch, keepCards, label) {
  if (running) return;
  branch = (branch || '').trim();
  if (!branch) {
    statusBox().textContent = '请填写目标分支名';
    $('switchBranch').focus();
    return;
  }
  if (!keepCards &&
      !confirm('确认批量切换到 ' + branch + '？\n会先 stash 当前未提交改动，再切到目标分支，并合并远程目标分支和远程主分支最新代码。\nstash 不会自动恢复到目标分支；有冲突会停在目标分支等待处理。')) return;
  begin(label || '切换分支中…', keepCards);
  let failMsg = '';
  const touched = new Set();
  try {
    await stream('/api/switch_branch',
      {projects: projectText, branch, base: $('base').value},
      (ev, d) => {
        if (ev === 'entries') d.entries.forEach(e => { touched.add(e.proj); makeCard(e.proj, e.target, 'switch'); });
        else if (ev === 'fatal') { failMsg = d.msg; }
        else if (ev === 'parse_error') { failMsg = '有无法解析的项目行: ' + d.line; }
        else if (ev === 'meta') setCur(d.proj, d.current_branch, d.dirty);
        else if (ev === 'log') {
          addLog(d.proj, d.level, d.msg);
          const el = cards[d.proj];
          if (el && el.classList.contains('running')) el.querySelector('.msg').textContent = d.msg;
        }
        else if (ev === 'result') setState(d.proj, d.state, d.msg, d.resume);
      });
  } catch (e) {
    failMsg = '切换分支连接中断: ' + e + '（请到各项目里 git status 确认状态）';
  } finally {
    for (const proj of touched) {
      if (cards[proj] && cards[proj].classList.contains('running'))
        setState(proj, 'error', '未返回结果');
    }
    end(false);
    statusBox().textContent = failMsg ||
      '切换完成。发生 stash 的项目，原分支改动已保存在 stash 恢复中心；冲突项解决后点「一键收尾」。';
  }
}

$('btnSwitch').onclick = () =>
  doSwitch($('switchProjects').value, $('switchBranch').value, false);

// ---------- 复制结果汇总 ----------
const STATE_ICON = {ok:'✅', clean:'🟢', uptodate:'⚪', conflict:'⚠️', error:'❌', running:'⏳', exists:'⚪'};
function copySummary() {
  const lines = ['分支处理结果：'];
  for (const proj of Object.keys(cards)) {
    const el = cards[proj];
    lines.push(`${STATE_ICON[el.dataset.state] || ''} ${proj}（${el.dataset.target}）：` +
               el.querySelector('.msg').textContent);
  }
  const text = lines.join('\n');
  const done = () => {
    const btn = copyButton();
    btn.textContent = '已复制 ✓';
    setTimeout(() => { btn.textContent = '复制结果汇总'; }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => { fallbackCopy(text); done(); });
  } else { fallbackCopy(text); done(); }
}
$('btnCopy').onclick = copySummary;
$('btnCopyCreate').onclick = copySummary;
$('btnCopySwitch').onclick = copySummary;
function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta);
  ta.select(); document.execCommand('copy'); ta.remove();
}

// ---------- stash 恢复中心 ----------
$('stashLink').onclick = openStash;
$('stashClose').onclick = () => { $('stashModal').style.display = 'none'; };
$('stashDetailClose').onclick = () => { $('stashDetailModal').style.display = 'none'; };
async function openStash() {
  $('stashModal').style.display = '';
  $('stashList').innerHTML = '<div class="hint">加载中…</div>';
  try {
    const r = await fetch('/api/stashes', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base: $('base').value})});
    const data = await r.json();
    if (!data.stashes.length) {
      $('stashList').innerHTML = '<div class="hint">没有待恢复的 stash，所有改动都在工作区里 ✓</div>';
      return;
    }
    $('stashList').innerHTML = '';
    for (const s of data.stashes) {
      const row = document.createElement('div');
      row.className = 'stashrow';
      row.innerHTML = `<b></b><span class="hint"></span><span style="flex:1" class="hint"></span><button class="secondary">查看详情</button><button>恢 复</button>`;
      row.children[0].textContent = s.proj;
      row.children[1].textContent = '属于分支 ' + s.branch + '（当前在 ' + (s.current || '?') + '）';
      row.children[2].textContent = s.date;
      row.children[3].onclick = () => openStashDetail(s);
      row.children[4].onclick = async () => {
        row.children[4].disabled = true;
        const rr = await fetch('/api/stash_pop', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({base: $('base').value, proj: s.proj, ref: s.ref})});
        const res = await rr.json();
        alert((res.ok ? '✅ ' : '❌ ') + s.proj + ': ' + res.msg);
        openStash();
      };
      $('stashList').appendChild(row);
    }
  } catch (e) {
    $('stashList').innerHTML = '<div class="hint">加载失败: ' + e + '</div>';
  }
}

async function openStashDetail(s) {
  $('stashDetailModal').style.display = '';
  $('stashDetailTitle').textContent = s.proj + ' — stash 详情';
  $('stashDetailMeta').textContent = s.ref + ' / 属于分支 ' + s.branch + ' / ' + s.date;
  setStashDetailHint('加载中…');
  try {
    const r = await fetch('/api/stash_detail', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({base: $('base').value, proj: s.proj, ref: s.ref})});
    const data = await r.json();
    if (!data.ok) {
      setStashDetailHint('读取失败: ' + data.msg);
      return;
    }
    if (!data.files.length) {
      setStashDetailHint('这个 stash 没有可展示的文件清单。');
      return;
    }
    const box = document.createElement('div');
    box.className = 'stashfiles';
    for (const f of data.files) {
      const item = document.createElement('div');
      item.className = 'stashfile';
      const status = document.createElement('b');
      const path = document.createElement('span');
      status.textContent = f.status;
      path.textContent = f.path;
      item.appendChild(status);
      item.appendChild(path);
      box.appendChild(item);
    }
    $('stashDetailBody').innerHTML = '';
    $('stashDetailBody').appendChild(box);
  } catch (e) {
    setStashDetailHint('加载失败: ' + e);
  }
}

function setStashDetailHint(text) {
  const hint = document.createElement('div');
  hint.className = 'hint';
  hint.textContent = text;
  $('stashDetailBody').innerHTML = '';
  $('stashDetailBody').appendChild(hint);
}

// ---------- 冲突解决器 ----------
const R = {proj:'', target:'', files:[], donefiles:new Set(), file:'', data:null, choices:[]};
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const api = (url, body) => fetch(url, {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify(Object.assign({base: $('base').value, proj: R.proj}, body))}).then(r => r.json());

$('resClose').onclick = () => { $('resModal').style.display = 'none'; };
$('resEditor').onclick = () => { if (R.file) api('/api/open_editor', {file: R.file}); };
$('resOursAll').onclick = () => fileSide('ours');
$('resTheirsAll').onclick = () => fileSide('theirs');

async function openResolver(proj, target) {
  R.proj = proj; R.target = target; R.donefiles = new Set();
  $('resTitle').textContent = proj + ' — 解决冲突';
  $('resHint').textContent = '';
  $('resModal').style.display = 'block';
  $('resMain').innerHTML = '<div class="hint" style="padding:20px">加载中…</div>';
  $('resFiles').innerHTML = '';
  const d = await api('/api/conflicts', {});
  R.files = d.files || [];
  if (!R.files.length) {
    $('resMain').innerHTML = `<div class="resdone">没有处于冲突状态的文件 ✓<br><br>
      <button onclick="$('resModal').style.display='none';doResume('${R.proj}','${R.target}')">直接一键收尾</button></div>`;
    setFootEnabled(false);
    return;
  }
  renderFileList();
  selectFile(R.files[0]);
}

function setFootEnabled(on) {
  ['resOursAll','resTheirsAll','resEditor'].forEach(id => $(id).disabled = !on);
  if (!on) $('resSave').disabled = true;
}

function renderFileList() {
  $('resFiles').innerHTML = '';
  for (const f of R.files) {
    const div = document.createElement('div');
    div.className = 'resfile' + (f === R.file ? ' active' : '') +
                    (R.donefiles.has(f) ? ' done' : '');
    div.textContent = (R.donefiles.has(f) ? '✓ ' : '') + f;
    div.onclick = () => { if (!R.donefiles.has(f)) selectFile(f); };
    $('resFiles').appendChild(div);
  }
}

async function selectFile(f) {
  R.file = f; R.choices = []; R.data = null;
  renderFileList();
  setFootEnabled(true);
  $('resMain').innerHTML = '<div class="hint" style="padding:20px">加载中…</div>';
  const d = await api('/api/conflict_detail', {file: f});
  if (d.binary) {
    $('resMain').innerHTML = '<div class="hint" style="padding:20px">该文件无法按文本解析（可能是二进制），请用底部按钮整体选边，或用编辑器处理。</div>';
    return;
  }
  R.data = d;
  let html = '', k = 0;
  for (const seg of d.segments) {
    if (seg.type === 'text') {
      html += `<pre class="ctx">${esc(seg.lines.join('\n'))}</pre>`;
    } else {
      R.choices.push(null);
      html += `
      <div class="cblock" id="cb${k}">
        <div class="cbar">冲突块 ${k+1}
          <button onclick="pick(${k},'ours')">用目标分支的</button>
          <button onclick="pick(${k},'theirs')">用主分支的</button>
          <button onclick="pick(${k},'both')">两个都要</button>
          <button onclick="pick(${k},'edit')">手动改</button>
        </div>
        <div class="cpane ours"><span class="plabel">目标分支（${esc(seg.ours_label)}）</span>${esc(seg.ours.join('\n'))}</div>
        <div class="cpane theirs"><span class="plabel">主分支（${esc(seg.theirs_label)}）</span>${esc(seg.theirs.join('\n'))}</div>
        <div class="cedit" style="display:none"><textarea spellcheck="false" autocorrect="off" autocapitalize="off" autocomplete="off" oninput="editChoice(${k}, this.value)"></textarea></div>
      </div>`;
      k++;
    }
  }
  $('resMain').innerHTML = html;
  updateSave();
}

function conflictSegs() { return R.data.segments.filter(s => s.type === 'conflict'); }

function pick(k, how) {
  const seg = conflictSegs()[k];
  const el = document.getElementById('cb' + k);
  el.classList.remove('picked-ours', 'picked-theirs');
  el.querySelectorAll('.cbar button').forEach((b, i) =>
    b.classList.toggle('on', ['ours','theirs','both','edit'][i] === how));
  const editBox = el.querySelector('.cedit');
  editBox.style.display = how === 'edit' ? '' : 'none';
  if (how === 'ours') { R.choices[k] = seg.ours.slice(); el.classList.add('picked-ours'); }
  else if (how === 'theirs') { R.choices[k] = seg.theirs.slice(); el.classList.add('picked-theirs'); }
  else if (how === 'both') { R.choices[k] = seg.ours.concat(seg.theirs); }
  else {
    const ta = editBox.querySelector('textarea');
    if (!ta.value) ta.value = seg.ours.concat(seg.theirs).join('\n');
    R.choices[k] = ta.value.split('\n');
    ta.focus();
  }
  updateSave();
}

function editChoice(k, value) { R.choices[k] = value.split('\n'); }

function updateSave() {
  $('resSave').disabled = R.choices.some(c => c === null);
}

$('resSave').onclick = async () => {
  if (!R.data) return;
  let lines = [], k = 0;
  for (const seg of R.data.segments) {
    if (seg.type === 'text') lines = lines.concat(seg.lines);
    else lines = lines.concat(R.choices[k++] || []);
  }
  const content = lines.join(R.data.eol) + (R.data.trailing ? R.data.eol : '');
  $('resSave').disabled = true;
  const r = await api('/api/conflict_save', {file: R.file, content});
  afterFileResolved(r);
};

async function fileSide(side) {
  if (!R.file) return;
  const r = await api('/api/conflict_side', {file: R.file, side});
  afterFileResolved(r);
}

function afterFileResolved(r) {
  if (!r.ok) { alert('❌ ' + r.msg); updateSave(); return; }
  R.donefiles.add(R.file);
  $('resHint').textContent = R.file + ' ' + r.msg + ' ✓';
  if (r.remaining === 0) {
    renderFileList();
    setFootEnabled(false);
    $('resMain').innerHTML = `<div class="resdone">全部冲突已解决 🎉<br><br>
      <button onclick="$('resModal').style.display='none';doResume('${R.proj}','${R.target}')">一键收尾（提交合并 → 推送 → 切回原分支）</button></div>`;
    return;
  }
  const next = R.files.find(f => !R.donefiles.has(f));
  if (next) selectFile(next); else renderFileList();
}

window.addEventListener('error', e => {
  statusBox().textContent = '页面脚本出错: ' + e.message;
  if (!running) { $('btnCheck').disabled = false; }
});
</script>
</body>
</html>
"""


# ---------------- 桌面 GUI（tkinter，零依赖） ----------------

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    CONF_FILE = os.path.expanduser("~/.sync-branches.json")
    STATE_TXT = {"clean": "✅ 可自动合并", "ok": "✅ 已完成", "uptodate": "⚪ 已是最新",
                 "conflict": "⚠️ 需人工处理", "error": "❌ 出错", "running": "⏳ 处理中…"}
    STATE_BG = {"clean": "#e3f5e9", "ok": "#e3f5e9", "uptodate": "#edeff2",
                "conflict": "#fcf0d8", "error": "#fbe3e3", "running": "#e6eefc"}

    def load_conf():
        try:
            with open(CONF_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    class App:
        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.logs = {}        # proj -> [log lines]
            self.row_state = {}   # proj -> 最新状态
            self.resumable = set()  # 可「冲突收尾」的项目
            self.running = False
            self.mode = ""        # check / sync / resume
            conf = load_conf()

            root.title("分支同步面板")
            root.geometry("920x680")
            root.minsize(760, 540)

            pad = {"padx": 12, "pady": 4}
            top = ttk.Frame(root)
            top.pack(fill="x", **pad)
            ttk.Label(top, text="仓库根目录").pack(side="left")
            self.base_var = tk.StringVar(value=conf.get("base") or WWW_DIR)
            ttk.Entry(top, textvariable=self.base_var).pack(
                side="left", fill="x", expand=True, padx=8)
            ttk.Button(top, text="选择…", command=self.pick_base).pack(side="left")

            ttk.Label(root, foreground="#8a919f",
                      text="「项目：分支」列表，一行一个，中英文冒号均可，例如  mix_ads_web：dev_ws_api_product"
                      ).pack(anchor="w", padx=12)
            self.list_text = tk.Text(root, height=6, font=("Menlo", 12),
                                     relief="solid", borderwidth=1,
                                     highlightthickness=0)
            self.list_text.pack(fill="x", **pad)
            if conf.get("list"):
                self.list_text.insert("1.0", conf["list"])

            btns = ttk.Frame(root)
            btns.pack(fill="x", **pad)
            self.btn_check = ttk.Button(btns, text="检 测（不动代码）",
                                        command=self.start_check)
            self.btn_check.pack(side="left")
            self.btn_sync = ttk.Button(btns, text="一键同步", state="disabled",
                                       command=self.start_sync)
            self.btn_sync.pack(side="left", padx=8)
            self.btn_sync_clean = ttk.Button(btns, text="只同步无冲突项",
                                             state="disabled",
                                             command=self.start_sync_clean)
            self.btn_sync_clean.pack(side="left")
            self.btn_resume = ttk.Button(btns, text="冲突收尾",
                                         command=self.start_resume)
            self.btn_resume.pack(side="left", padx=8)
            self.status_var = tk.StringVar(value="先点「检测」预演合并，绿色项再一键同步。")
            ttk.Label(btns, textvariable=self.status_var,
                      foreground="#8a919f").pack(side="left", padx=8)

            btns2 = ttk.Frame(root)
            btns2.pack(fill="x", padx=12)
            ttk.Button(btns2, text="重试选中项",
                       command=self.retry_selected).pack(side="left")
            ttk.Button(btns2, text="复制结果汇总",
                       command=self.copy_summary).pack(side="left", padx=8)
            ttk.Button(btns2, text="stash 恢复中心",
                       command=self.open_stash_center).pack(side="left")
            ttk.Button(btns2, text="解决冲突（选边）",
                       command=self.open_conflict_fix).pack(side="left", padx=8)

            cols = ("proj", "branch", "cur", "state", "msg")
            self.tree = ttk.Treeview(root, columns=cols, show="headings")
            for col, txt, w in (("proj", "项目", 160), ("branch", "目标分支", 150),
                                ("cur", "当前分支", 150),
                                ("state", "状态", 120), ("msg", "说明", 340)):
                self.tree.heading(col, text=txt)
                self.tree.column(col, width=w, anchor="w")
            for st, bg in STATE_BG.items():
                self.tree.tag_configure(st, background=bg)
            self.tree.pack(fill="both", expand=True, padx=12, pady=(2, 4))
            self.tree.bind("<Double-1>", self.show_detail)
            ttk.Label(root, text="双击一行查看详细日志（冲突的处理步骤也在里面）",
                      foreground="#8a919f").pack(anchor="w", padx=12, pady=(0, 8))

            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.pump()

        # ---------- 基础 ----------
        def pick_base(self):
            d = filedialog.askdirectory(initialdir=self.base_var.get() or "~")
            if d:
                self.base_var.set(d)

        def save_conf(self):
            try:
                with open(CONF_FILE, "w", encoding="utf-8") as f:
                    json.dump({"base": self.base_var.get(),
                               "list": self.list_text.get("1.0", "end").strip()},
                              f, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass

        def on_close(self):
            if self.running and not messagebox.askokcancel(
                    "确认退出", "任务还在执行中，确定要退出吗？"):
                return
            self.save_conf()
            self.root.destroy()

        def get_entries(self):
            base = self.base_var.get().strip() or WWW_DIR
            if not any_base_dir_exists(base):
                messagebox.showerror("目录不存在", "仓库根目录不存在:\n%s" % base)
                return None, None
            entries, errors = parse_entries(self.list_text.get("1.0", "end"))
            if errors:
                messagebox.showwarning("有无法解析的行", "\n".join(errors))
            if not entries:
                messagebox.showinfo("提示", "请先粘贴「项目：分支」列表")
                return None, None
            return entries, base

        def fill_rows(self, entries):
            self.tree.delete(*self.tree.get_children())
            self.logs = {}
            self.row_state = {}
            for p, t in entries:
                self.logs[p] = []
                self.row_state[p] = "running"
                self.tree.insert("", "end", iid=p, values=(
                    p, t, "…", STATE_TXT["running"], ""), tags=("running",))

        def set_row(self, proj, state, msg):
            if self.tree.exists(proj):
                p, t, cur = self.tree.item(proj, "values")[:3]
                if state == "error" and cur == "…":
                    cur = ""
                self.row_state[proj] = state
                self.tree.item(proj, values=(p, t, cur,
                                             STATE_TXT.get(state, state), msg),
                               tags=(state,))

        def set_cur(self, proj, branch, dirty):
            if self.tree.exists(proj) and branch:
                v = list(self.tree.item(proj, "values"))
                v[2] = branch + ("（有改动）" if dirty else "")
                self.tree.item(proj, values=v)

        def busy(self, on, label=""):
            self.running = on
            self.btn_check.config(state="disabled" if on else "normal")
            self.btn_sync.config(state="disabled")
            self.btn_sync_clean.config(state="disabled")
            self.btn_resume.config(state="disabled" if on else "normal")
            if label:
                self.status_var.set(label)

        def get_base(self):
            base = self.base_var.get().strip() or WWW_DIR
            if not any_base_dir_exists(base):
                messagebox.showerror("目录不存在", "仓库根目录不存在:\n%s" % base)
                return None
            return base

        # ---------- 检测 ----------
        def start_check(self):
            if self.running:
                return
            entries, base = self.get_entries()
            if not entries:
                return
            self.save_conf()
            self.fill_rows(entries)
            self.busy(True, "检测中（不会改动任何代码）…")
            self.mode = "check"
            emit = lambda n, d: self.q.put((n, d))  # noqa: E731
            threading.Thread(target=sse_check, args=(entries, emit, base),
                             daemon=True).start()

        # ---------- 同步 ----------
        def start_sync(self):
            if self.running:
                return
            entries, base = self.get_entries()
            if not entries:
                return
            if not messagebox.askokcancel(
                    "确认同步",
                    "将合并主分支并推送到远程。\n"
                    "有冲突的项目会停在冲突现场等你手动处理。\n确认执行？"):
                return
            self.save_conf()
            self.fill_rows(entries)
            self.busy(True, "同步中…")
            self.mode = "sync"
            emit = lambda n, d: self.q.put((n, d))  # noqa: E731
            threading.Thread(target=sse_sync, args=(entries, emit, base),
                             daemon=True).start()

        # ---------- 局部同步（只同步无冲突项 / 单项重试） ----------
        def run_partial_sync(self, entries, label):
            base = self.get_base()
            if not base:
                return
            for p, _t in entries:
                self.logs[p] = []
                self.set_row(p, "running", "")
            self.busy(True, label)
            self.mode = "sync"
            emit = lambda n, d: self.q.put((n, d))  # noqa: E731
            threading.Thread(target=sse_sync, args=(entries, emit, base),
                             daemon=True).start()

        def start_sync_clean(self):
            if self.running:
                return
            entries = [(p, self.tree.item(p, "values")[1])
                       for p in self.tree.get_children()
                       if self.row_state.get(p) == "clean"]
            if not entries:
                messagebox.showinfo("提示", "没有「可自动合并」状态的项目。")
                return
            self.run_partial_sync(entries, "同步无冲突项…")

        def retry_selected(self):
            if self.running:
                return
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先在表格里选中要重试的项目。")
                return
            proj = sel[0]
            self.run_partial_sync([(proj, self.tree.item(proj, "values")[1])],
                                  "重试 %s …" % proj)

        # ---------- 复制结果汇总 ----------
        def copy_summary(self):
            rows = self.tree.get_children()
            if not rows:
                messagebox.showinfo("提示", "还没有结果，先点「检测」或「一键同步」。")
                return
            icon = {"clean": "🟢", "ok": "✅", "uptodate": "⚪",
                    "conflict": "⚠️", "error": "❌", "running": "⏳"}
            lines = ["分支同步结果："]
            for iid in rows:
                p, t, _c, s, m = self.tree.item(iid, "values")
                lines.append("%s %s（%s）：%s" % (
                    icon.get(self.row_state.get(iid, ""), ""), p, t, m or s))
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
            self.status_var.set("结果汇总已复制到剪贴板 ✓")

        # ---------- stash 恢复中心 ----------
        def open_stash_center(self):
            base = self.get_base()
            if not base:
                return
            win = tk.Toplevel(self.root)
            win.title("stash 恢复中心")
            win.geometry("640x380")
            ttk.Label(win, foreground="#8a919f",
                      text="工具自动 stash 的改动都在这里。「恢复选中」会先切回改动所属的分支再取出。"
                      ).pack(anchor="w", padx=10, pady=(10, 4))
            cols = ("proj", "branch", "current", "date")
            tv = ttk.Treeview(win, columns=cols, show="headings")
            for col, txt, w in (("proj", "项目", 150), ("branch", "属于分支", 150),
                                ("current", "当前分支", 130), ("date", "时间", 140)):
                tv.heading(col, text=txt)
                tv.column(col, width=w, anchor="w")
            tv.pack(fill="both", expand=True, padx=10)
            refs = {}

            def reload_list():
                tv.delete(*tv.get_children())
                refs.clear()
                for s in list_stashes(base):
                    iid = "%s|%s" % (s["proj"], s["ref"])
                    refs[iid] = (s["proj"], s["ref"])
                    tv.insert("", "end", iid=iid, values=(
                        s["proj"], s["branch"], s["current"] or "?", s["date"]))
                if not refs:
                    tv.insert("", "end", values=(
                        "（没有待恢复的 stash ✓）", "", "", ""))

            def do_pop():
                sel = tv.selection()
                if not sel or sel[0] not in refs:
                    messagebox.showinfo("提示", "请选中一条 stash。", parent=win)
                    return
                proj, ref = refs[sel[0]]
                ok, msg = pop_stash(base, proj, ref)
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "恢复结果", "%s: %s" % (proj, msg), parent=win)
                reload_list()

            def show_stash_detail():
                sel = tv.selection()
                if not sel or sel[0] not in refs:
                    messagebox.showinfo("提示", "请选中一条 stash。", parent=win)
                    return
                proj, ref = refs[sel[0]]
                detail = stash_detail(base, proj, ref)
                if not detail.get("ok"):
                    messagebox.showerror("读取失败", detail.get("msg", "读取 stash 详情失败"), parent=win)
                    return

                detail_win = tk.Toplevel(win)
                detail_win.title("%s — stash 详情" % proj)
                detail_win.geometry("620x360")
                ttk.Label(detail_win, foreground="#8a919f",
                          text="%s / %s" % (proj, ref)).pack(anchor="w", padx=10, pady=(10, 4))
                txt = tk.Text(detail_win, font=("Menlo", 12), wrap="none")
                txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
                files = detail.get("files") or []
                if files:
                    txt.insert("1.0", "\n".join(
                        "%-8s %s" % (f["status"], f["path"]) for f in files))
                else:
                    txt.insert("1.0", "这个 stash 没有可展示的文件清单。")
                txt.config(state="disabled")

            bar = ttk.Frame(win)
            bar.pack(fill="x", padx=10, pady=8)
            ttk.Button(bar, text="恢复选中", command=do_pop).pack(side="left")
            ttk.Button(bar, text="查看详情", command=show_stash_detail).pack(side="left", padx=8)
            ttk.Button(bar, text="刷新", command=reload_list).pack(side="left")
            reload_list()

        # ---------- 解决冲突（文件级选边） ----------
        def open_conflict_fix(self):
            sel = self.tree.selection()
            if not sel or sel[0] not in self.resumable:
                messagebox.showinfo("提示", "请先选中一个「需人工处理」的项目。")
                return
            base = self.get_base()
            if not base:
                return
            proj = sel[0]
            win = tk.Toplevel(self.root)
            win.title("%s — 解决冲突" % proj)
            win.geometry("560x360")
            ttk.Label(win, foreground="#8a919f",
                      text="逐个文件整体选边；需要逐行合并的请「用编辑器打开」，"
                           "改完保存后点「刷新」，全部解决后回主窗口点「冲突收尾」。"
                      ).pack(anchor="w", padx=10, pady=(10, 4))
            lb = tk.Listbox(win, font=("Menlo", 11))
            lb.pack(fill="both", expand=True, padx=10)

            def reload_files():
                lb.delete(0, "end")
                for f in conflict_files(base, proj) or []:
                    lb.insert("end", f)
                if lb.size() == 0:
                    lb.insert("end", "（没有冲突文件了 ✓ 回主窗口点「冲突收尾」）")

            def act(side):
                if not lb.curselection():
                    messagebox.showinfo("提示", "先选中一个文件。", parent=win)
                    return
                f = lb.get(lb.curselection()[0])
                if f.startswith("（"):
                    return
                if side == "editor":
                    open_in_editor(base, proj, f)
                    return
                ok, msg, _rem = resolve_file_side(base, proj, f, side)
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "结果", "%s: %s" % (f, msg), parent=win)
                reload_files()

            bar = ttk.Frame(win)
            bar.pack(fill="x", padx=10, pady=8)
            ttk.Button(bar, text="用目标分支版本",
                       command=lambda: act("ours")).pack(side="left")
            ttk.Button(bar, text="用主分支版本",
                       command=lambda: act("theirs")).pack(side="left", padx=8)
            ttk.Button(bar, text="用编辑器打开",
                       command=lambda: act("editor")).pack(side="left")
            ttk.Button(bar, text="刷新",
                       command=reload_files).pack(side="left", padx=8)
            reload_files()

        # ---------- 冲突收尾 ----------
        def start_resume(self):
            if self.running:
                return
            sel = self.tree.selection()
            if not sel or sel[0] not in self.resumable:
                messagebox.showinfo(
                    "提示", "请先在表格里选中一个「需人工处理」的项目。\n"
                           "（解决完冲突文件并 git add 之后再点收尾）")
                return
            proj = sel[0]
            target = self.tree.item(proj, "values")[1]
            base = self.base_var.get().strip() or WWW_DIR
            if not any_base_dir_exists(base):
                messagebox.showerror("目录不存在", "仓库根目录不存在:\n%s" % base)
                return
            self.busy(True, "收尾中：提交合并、推送、切回原分支…")
            self.mode = "resume"
            self.set_row(proj, "running", "收尾中…")
            emit = lambda n, d: self.q.put((n, d))  # noqa: E731
            threading.Thread(target=sse_resume, args=(proj, target, emit, base),
                             daemon=True).start()

        # ---------- 事件泵 ----------
        def pump(self):
            try:
                while True:
                    name, d = self.q.get_nowait()
                    self.handle(name, d)
            except queue.Empty:
                pass
            self.root.after(80, self.pump)

        def handle(self, name, d):
            if name == "check":
                proj = d["proj"]
                self.set_row(proj, d["state"], d["msg"])
                self.set_cur(proj, d.get("current_branch"), d.get("dirty"))
                self.mark_resumable(proj, d.get("resume"))
                lines = list(d.get("logs") or [])
                if d.get("conflict_files"):
                    lines.append("冲突文件:")
                    lines += ["  " + f for f in d["conflict_files"]]
                if d.get("dirty"):
                    lines.append("当前分支 %s 有未提交改动，同步时会自动 stash 并恢复"
                                 % d.get("current_branch", ""))
                self.logs.setdefault(proj, []).extend(lines)
            elif name == "meta":
                self.set_cur(d["proj"], d.get("current_branch"), d.get("dirty"))
            elif name == "log":
                self.logs.setdefault(d["proj"], []).append(d["msg"])
                if self.tree.exists(d["proj"]) and \
                        "running" in self.tree.item(d["proj"], "tags"):
                    p, t, c, s, _ = self.tree.item(d["proj"], "values")
                    self.tree.item(d["proj"], values=(p, t, c, s, d["msg"]))
            elif name == "result":
                self.set_row(d["proj"], d["state"], d["msg"])
                self.mark_resumable(d["proj"], d.get("resume"))
            elif name == "done":
                mode = self.mode
                self.busy(False)
                if mode == "check" and self.tree.get_children():
                    self.btn_sync.config(state="normal")
                    if any(v == "clean" for v in self.row_state.values()):
                        self.btn_sync_clean.config(state="normal")
                self.status_var.set({
                    "check": "检测完成。绿色项可放心一键同步；黄色项会停在冲突现场等你处理。",
                    "sync": "同步完成。冲突项解决文件后选中该行点「冲突收尾」。",
                    "resume": "收尾完成。",
                }.get(mode, ""))

        def mark_resumable(self, proj, flag):
            if flag:
                self.resumable.add(proj)
            else:
                self.resumable.discard(proj)

        # ---------- 详情 ----------
        def show_detail(self, _event):
            sel = self.tree.selection()
            if not sel:
                return
            proj = sel[0]
            win = tk.Toplevel(self.root)
            win.title("%s — 详细日志" % proj)
            win.geometry("640x420")
            txt = tk.Text(win, font=("Menlo", 12), wrap="word")
            txt.pack(fill="both", expand=True, padx=8, pady=8)
            txt.insert("1.0", "\n".join(self.logs.get(proj) or ["（暂无日志）"]))
            txt.config(state="disabled")

    root = tk.Tk()
    App(root)
    root.mainloop()


def main():
    if GUI_MODE:
        run_gui()
        return
    url = "http://127.0.0.1:%d/sync" % PORT
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # 端口被占用：大概率是面板已经在运行，直接打开浏览器即可
        print("端口 %d 已被占用（面板可能已在运行），直接打开 %s" % (PORT, url))
        if not os.environ.get("SYNC_NO_BROWSER"):
            webbrowser.open(url)
        return
    print("分支同步面板已启动: %s  （Ctrl-C 退出）" % url)
    if not os.environ.get("SYNC_NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
