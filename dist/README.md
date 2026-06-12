# 分支同步面板

批量把多个 git 项目的指定分支合并上主分支最新代码并推送，可视化看到哪些项目会冲突需要人工处理。
**原生桌面窗口应用**（不依赖浏览器），关闭窗口即退出。

测试同学发来这样的列表，直接整段粘贴进面板即可（中英文冒号都行）：

```
mix_ads_web：dev_ws_api_product
mix_ads_ws：dev_ws_api_product
rpc_process：dev_ws_api_product
```

## 使用流程

1. 填写**仓库根目录**（存放各个项目文件夹的目录，可点「选择…」浏览），会自动记住
2. 粘贴「项目：分支」列表
3. 点**检测**：不动任何代码，预演合并 —— ✅ 可自动合并 / ⚠️ 会冲突需人工 / ⚪ 已是最新 / ❌ 出错
4. 点**一键同步**：自动 stash 你的改动 → 切目标分支 → 合并主分支 → 推送 → 切回原分支恢复改动；
   有冲突的项目会停在冲突现场，**双击该行**查看解决步骤

## 安装

依赖：git ≥ 2.38、Python 3（macOS 自带；面板只监听本机 127.0.0.1，不会暴露到网络）

### macOS

把 `分支同步面板.app` 拖到「应用程序」文件夹，双击运行。

> 第一次打开如果提示「无法验证开发者」：右键点击 App → 打开 → 再点「打开」。
> 之后双击即可，关闭窗口就是退出。

### Windows

把 `windows` 文件夹拷到任意位置（两个文件保持在一起），双击 `分支同步面板.bat`。

- 需要已安装 [Git for Windows](https://git-scm.com/download/win) 和 [Python 3](https://www.python.org/downloads/)（装 Python 时勾选 *Add Python to PATH*，自带 tkinter）

## 其他模式（可选）

- 网页模式：`python3 sync-branches-ui.py`（不带 `--gui`），浏览器打开 http://127.0.0.1:8799
- 命令行版（macOS/Linux）：

```
SYNC_BASE_DIR=~/你的项目目录 ./sync-branches.sh
# 然后粘贴列表，Ctrl-D 结束
```
