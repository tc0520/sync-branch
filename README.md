# 分支同步面板（sync-branches）

批量把多个 git 项目的指定分支合并上主分支最新代码并推送的桌面工具。
典型场景：测试同学发来一串「项目：分支」，以前要逐个项目 stash → 切分支 → 拉代码 → 合并 → 推送 → 切回来；现在粘贴进面板一键完成，冲突也能在面板里点选解决。

```
mix_ads_web：dev_ws_api_product
mix_ads_ws：dev_ws_api_product
rpc_process：dev_ws_api_product
```

## 功能一览

- **检测**（不动任何代码）：用 `git merge-tree` 预演合并，标出 🟢可自动合并 / ⚠️会冲突（列出冲突文件）/ ⚪已是最新 / ❌出错，并显示每个项目的当前分支、未提交改动、落后主分支几个提交
- **一键同步**（多项目并行）：stash 当前改动 → 切目标分支 → 合并远程目标分支 → 合并主分支 → 推送 → 切回原分支 → 恢复 stash；冲突的项目保留现场停在目标分支
- **只同步无冲突项 / 单项重试**
- **冲突可视化解决**：逐个冲突块「用目标分支的 / 用主分支的 / 两个都要 / 手动改」，也可整文件选边或调起 VS Code；解决完**一键收尾**（提交合并 → 推送 → 切回 → 恢复改动）
- **stash 恢复中心**：列出工具自动 stash 的所有改动，一键找回，防止丢改动
- **复制结果汇总**：生成纯文字报告粘回给测试同学
- 主分支按 `origin/HEAD` 自动识别（master / main 混用没关系）

## 使用方式（四选一）

| 方式 | 怎么用 | 适用 |
|---|---|---|
| Mac 应用 | 双击 `dist/分支同步面板.app`（可拖进「应用程序」） | 日常推荐 |
| Windows | 把 `dist/windows/` 两个文件拷出去，双击 `分支同步面板.bat` | Windows 同事 |
| 网页 | `python3 sync-branches-ui.py`，浏览器开 http://127.0.0.1:8799 | 临时/远程 |
| 命令行 | `SYNC_BASE_DIR=~/项目目录 ./sync-branches.sh` 然后粘贴列表 Ctrl-D | 不想开界面 |

依赖：git ≥ 2.38（要用 `merge-tree --write-tree`）、Python 3.9+。只监听 127.0.0.1，不暴露网络。
发给同事：直接把 `dist/分支同步面板.zip` 发出去（里面含 Mac 应用、Windows 包和用户版说明）。
Mac 首次打开提示「无法验证开发者」时：右键 App → 打开。

## 目录结构

```
sync-branches/
├── sync-branches-ui.py    ★ 核心源码（唯一需要日常修改的文件）
├── sync-branches.sh       命令行版（独立实现，bash 3.2 兼容）
├── macos/main.swift       Mac 原生窗口壳（WKWebView 加载本地服务）
├── scripts/build.sh       一键构建：编译壳 + 刷新拷贝 + 签名 + 打 zip
└── dist/                  分发产物
    ├── 分支同步面板.app    Mac 应用（Resources 里有 ui.py 的构建拷贝）
    ├── windows/           Windows 包（.bat + ui.py 的构建拷贝）
    ├── README.md          用户版说明（会进 zip）
    └── 分支同步面板.zip    发给同事的包（build.sh 生成）
```

## 架构（改代码前先看这段）

**`sync-branches-ui.py` 单文件三层**，从上到下：

1. **引擎层**（纯函数，git 子进程封装）：
   `check_one` 预检 / `sync_one` 同步 / `resume_one` 冲突收尾 /
   `parse_conflict_file`·`save_resolution`·`resolve_file_side` 冲突解决 /
   `list_stashes`·`pop_stash` stash 中心。
   所有函数通过 `emit(event, data)` 回调上报进度，**不感知界面**。
2. **Web 层**：`ThreadingHTTPServer` + 内嵌 HTML（`PAGE` 变量）。
   流式接口走 SSE：`/api/check`、`/api/sync`、`/api/resume`；
   普通 JSON：`/api/stashes`、`/api/stash_pop`、`/api/conflicts`、
   `/api/conflict_detail`、`/api/conflict_save`、`/api/conflict_side`、`/api/open_editor`。
   SSE 事件类型：`entries / meta / log / result / check / parse_error / fatal / done`，
   其中 result 可带 `resume: true` 表示该项目可走冲突解决/收尾。
3. **GUI 层**（`run_gui`，tkinter）：仅给 Windows 用（Mac 系统 Tk 8.5 有白屏 bug，所以 Mac 走 Swift 壳 + Web 层）。功能比 Web 层略简：冲突解决只有文件级选边。

**关键约定：**

- 入口：`--gui` 进 tkinter；否则起 Web 服务（端口默认 8799，可传数字参数改）
- 环境变量：`SYNC_DEFAULT_BASE` 默认仓库根目录（壳启动器会设为 $HOME）；`SYNC_NO_BROWSER=1` 不自动开浏览器（Swift 壳用）
- 冲突现场的收尾信息（出发分支、是否 stash 过）写在 `<repo>/.git/sync-branches-resume.json`
- 工具创建的 stash 统一带 `sync-branches-auto: <原分支名>` 标记，stash 恢复中心靠它识别
- 同名项目在一次同步里只会跑一个线程（重复条目自动跳过）
- 客户端传来的文件路径必须经 `repo_file_path()` 校验（防 `../` 越界）
- `sync-branches.sh` 是独立实现，改流程语义时**两边都要改**；它的 `SYNC_PORCELAIN=1` 输出 `@@LOG|项目|级别|消息`、`@@RESULT|项目|状态|消息` 机器格式

## 开发与调试

```bash
# 改完直接跑，浏览器里调（带热改：改完重启脚本+刷新页面即可）
python3 sync-branches-ui.py 8799

# 调 Windows GUI（Mac 上能跑起来但渲染可能白屏，逻辑调试够用）
python3 sync-branches-ui.py --gui
```

**测试方法**：不要拿真实仓库试，几条命令就能搭个沙盒——建一个 bare 仓库当远程，clone 两份分别造出「目标分支」和「主分支前进/冲突」的提交，再用 curl 打 API 断言结果。冲突场景：两个分支改同一文件同一行。曾用的断言点：远程分支是否包含主分支（`merge-base --is-ancestor`）、是否切回原分支、stash 是否恢复/保留、冲突现场 `.git/MERGE_HEAD` 是否存在。

**已知的坑（都踩过）：**

- bash 3.2 里 `"$变量中文标点"` 紧邻会在无 locale 的子进程下报 `unbound variable`，必须写 `${变量}中文`
- macOS 自带 Python 的 Tk 是 8.5，新系统上窗口白屏——这就是 Mac 端用 Swift 壳的原因，别试图换回 tkinter
- 纯代码起的 Mac 应用必须挂 `NSMenu`，否则 Cmd+C/V 不工作（见 `macos/main.swift` 的 `buildMainMenu`）
- Apple Silicon 上 .app 必须至少 ad-hoc 签名（build.sh 已处理）
- WKWebView 里 `confirm()` 需要实现 `WKUIDelegate`，否则永远返回 false

## 构建与发布

```bash
./scripts/build.sh    # 编译壳 -> 刷新两份 ui.py 拷贝 -> 签名 -> 生成 dist/分支同步面板.zip
```

> ⚠️ `dist/分支同步面板.app/Contents/Resources/` 和 `dist/windows/` 里的 `sync-branches-ui.py`
> 是**构建拷贝**，永远不要直接改它们——改根目录那份，然后跑 build.sh。

发布就是把新的 `dist/分支同步面板.zip` 发给同事；Mac 同事重新拖一次 .app，Windows 同事替换 `sync-branches-ui.py` 即可。

## 推到 GitLab（可选）

```bash
git remote add origin http://gitlab.standard-software.co/<你的组>/sync-branches.git
git push -u origin master
```
