# 分支同步面板

批量把多个 git 项目的指定分支合并上主分支最新代码并推送，也能基于远程主分支批量创建新分支，或批量切换多个项目到同一个目标分支，可视化看到哪些项目会冲突需要人工处理。
推荐使用 Electron 桌面应用；它自带 Python 后端，macOS 和 Windows 都是同一套桌面体验，包含「同步分支」「创建新分支」「切换分支」三个页面。

测试同学发来这样的列表，直接整段粘贴进面板即可（中英文冒号都行）：

```
mix_ads_web：dev_ws_api_product
mix_ads_ws：dev_ws_api_product
rpc_process：dev_ws_api_product
```

## 使用流程

1. 填写**仓库根目录**（存放各个项目文件夹的目录，可点「选择…」浏览），会自动记住；多个目录可用逗号、分号或换行分隔
2. 粘贴「项目：分支」列表
3. 点**检测**：不动任何代码，预演合并 —— ✅ 可自动合并 / ⚠️ 会冲突需人工 / ⚪ 已是最新 / ❌ 出错
4. 点**一键同步**：自动 stash 你的改动 → 切目标分支 → 合并主分支 → 推送 → 切回原分支恢复改动；
   有冲突的项目会停在冲突现场，**双击该行**查看解决步骤

## 创建新分支

接到新需求时，可以切到「创建新分支」页面：

1. 粘贴项目列表，一行一个项目名
2. 填写新分支名
3. 选择是否推送到远程并设置 upstream
4. 点**创建分支**

工具会基于远程主分支创建并切到新分支。如果当前分支有未提交改动，会先 stash 保存，但不会恢复到新分支，避免把旧需求改动带过去。不推送时不会把 upstream 绑到主分支；需要推送时再执行 `git push -u origin <分支名>`。本地已存在同名分支时会直接切过去；远程已存在但本地没有时，会拉到本地并切过去。

## 切换分支

接到需要让多个项目切到同一个已有分支时，可以切到「切换分支」页面：

1. 粘贴项目列表，一行一个项目名
2. 填写目标分支名
3. 点**切换分支**

工具会先 stash 当前未提交改动，再切到目标分支，并合并远程目标分支和远程主分支最新代码。切换完成后会保持在目标分支，不推送、不切回、不自动恢复 stash。stash 恢复中心会显示改动来自哪个来源分支。

## 多个项目目录

如果代码分散在多个目录，在「仓库根目录」里按顺序填写多个目录即可，例如：

```
/Applications/ServBay/www;/Users/you/company-extra
```

选择项目、创建分支、切换分支、同步分支都会按这个顺序查找项目。如果不同目录里有同名项目，默认使用第一个目录里的项目。

## 安装

依赖：git ≥ 2.38。Electron 应用自带 Python 后端，不需要单独安装 Python；面板只监听本机 127.0.0.1，不会暴露到网络。

## Electron 应用

直接双击安装包或解压后的应用即可。重复双击会聚焦已有窗口，不会反复打开终端或启动多个后端服务。

如果使用旧版 Windows `start.bat` 包，仍然需要系统安装 Python 3；建议优先换成 Electron 包。

### macOS

把 Electron 生成的 `分支同步面板.app` 拖到「应用程序」文件夹，双击运行。

> 第一次打开如果提示「无法验证开发者」：右键点击 App → 打开 → 再点「打开」。
> 之后双击即可，关闭窗口就是退出。

### Windows

优先使用 Electron 生成的 Windows 安装包或 zip 包。旧版 `windows` 文件夹仍可使用：把文件夹拷到任意位置（两个文件保持在一起），双击 `分支同步面板.bat`，会打开本机 Web 面板。

- 需要已安装 [Git for Windows](https://git-scm.com/download/win) 和 [Python 3](https://www.python.org/downloads/)（装 Python 时勾选 *Add Python to PATH*）

## 其他模式（可选）

- 网页模式：`python3 sync-branches-ui.py`（不带 `--gui`），浏览器打开 http://127.0.0.1:8799/sync、http://127.0.0.1:8799/create 或 http://127.0.0.1:8799/switch
- 命令行版（macOS/Linux）：

```
SYNC_BASE_DIR=~/你的项目目录 ./sync-branches.sh
# 然后粘贴列表，Ctrl-D 结束

SYNC_BASE_DIR=~/你的项目目录 ./sync-branches.sh --create dev_new_requirement --push
# 然后粘贴项目名列表，一行一个项目，Ctrl-D 结束

SYNC_BASE_DIR=~/你的项目目录 ./sync-branches.sh --switch dev_requirement
# 然后粘贴项目名列表，一行一个项目，Ctrl-D 结束
```

命令行版也支持多个目录：

```
SYNC_BASE_DIR="/Applications/ServBay/www;/Users/you/company-extra" ./sync-branches.sh
```
