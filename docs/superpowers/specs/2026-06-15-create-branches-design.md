# 创建新分支功能设计

## 背景

现有工具用于把多个 Git 项目的目标分支同步主分支最新代码。新需求是在接到一个新需求时，批量基于远程主分支为多个项目创建同名新分支，并把各项目工作区切到新分支。

## 目标

- 支持一次输入多个项目名和一个新分支名。
- 每个项目基于远程主分支创建新分支。
- 创建成功后停留在新分支。
- 如果创建前当前分支有未提交修改，先 stash 保存到当前分支语义下，创建后不自动恢复到新分支。
- 支持是否推送到远程并设置 upstream，由界面勾选控制。
- 如果本地或远程已存在同名分支，跳过该项目，不覆盖、不 reset、不删除重建。

## 非目标

- 不把旧分支未提交修改自动 pop 到新分支。
- 不处理已存在分支的切换或复用。
- 不执行 force push、删除远程分支、reset 分支等危险操作。
- 不改变现有同步分支流程的语义。

## 用户界面

在现有 Web 面板中新增“创建新分支”区域，复用仓库根目录输入。该区域包含：

- 项目列表：一行一个项目名，例如 `mix_ads_web`、`mix_ads_ws`。
- 新分支名：单独输入一次，例如 `dev_new_requirement`。
- 推送选项：勾选后执行 `git push -u origin <new_branch>`；不勾选则只创建本地分支。
- 创建按钮：触发批量创建，结果和日志按项目流式展示。

Mac App 使用同一个 Web 面板，因此自动获得该功能。Windows 分发包里的 `.bat` 运行同一个 Web 面板，因此也获得该功能。`--gui` tkinter 入口保持现有同步能力，不在本次范围内新增创建分支界面。

## 后端接口

在 `sync-branches-ui.py` 中新增创建分支的解析、执行和 SSE 接口：

- 解析项目列表：忽略空行和注释行，每行只取项目名。
- SSE 接口为 `/api/create_branch`。
- 请求字段：
  - `projects`: 项目列表文本。
  - `branch`: 新分支名。
  - `base`: 仓库根目录。
  - `push`: 是否推送远程。
- 事件沿用现有风格：`entries`、`log`、`result`、`parse_error`、`fatal`、`done`。

## 创建流程

每个项目独立执行：

1. 校验项目目录存在。
2. 校验目录是 Git 仓库。
3. 如果存在未完成 merge 或 rebase 状态，报错并跳过。
4. 记录当前分支；detached HEAD 时记录短 commit 并给出警告。
5. 如果工作区有未提交修改，执行 `git stash push -u -m "sync-branches-create: <orig>"`，后续不自动 `stash pop`。
6. 执行 `git fetch origin --prune`。
7. 识别远程主分支：优先 `origin/HEAD`，再尝试 `origin/master`、`origin/main`。
8. 如果本地 `refs/heads/<branch>` 或远程 `refs/remotes/origin/<branch>` 已存在，返回 `exists`，不切换、不覆盖。
9. 执行 `git checkout -q -b <branch> origin/<main>`。
10. 如果用户勾选推送，执行 `git push -u origin <branch>`。
11. 返回成功结果；项目停留在新分支。

## 结果语义

- `ok`: 分支创建成功；如果已推送，说明远程 upstream 已设置。
- `exists`: 本地或远程已有同名分支，已跳过。
- `error`: 目录不存在、非 Git 仓库、已有 merge/rebase、stash 失败、fetch 失败、主分支识别失败、checkout 失败、push 失败等。

当项目发生 stash 时，日志和结果中都应明确提示“旧改动已保存在 stash 中，未恢复到新分支”。

## 命令行支持

为保持现有 README 中“Web 与 shell 独立实现”的约定，`sync-branches.sh` 增加创建分支模式。命令行接口为：

```bash
SYNC_BASE_DIR=~/projects ./sync-branches.sh --create dev_new_requirement
SYNC_BASE_DIR=~/projects ./sync-branches.sh --create dev_new_requirement --push
```

标准输入为一行一个项目名。命令行流程与 Web 引擎一致。

## 测试

使用临时 Git 沙盒验证：

- 多项目基于 `origin/HEAD` 创建同名新分支。
- 勾选推送时远程分支存在且本地 upstream 正确。
- 不勾选推送时只存在本地分支。
- 当前工作区有未提交修改时，创建前会 stash，创建后停留在新分支且改动不在工作区。
- 本地或远程已有同名分支时返回 `exists`，不覆盖原分支。
- merge/rebase 未完成状态返回错误。
- 主分支为 `master` 和 `main` 都能识别。
