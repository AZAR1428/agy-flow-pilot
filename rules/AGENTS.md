# Flow Pilot · 读写路由、进度播报与 Windows PS 5.1 规范

## 1. 读写与检索路由
- **常规/已知行段**：单文件或已知行区间（`<=800` 行 / `46 KB`）同回合并行调原生 `read_file`。
- **探索与大文件**：多词搜索用 `fast_io_grep`，`>800` 行大文件先用 `fast_io_outline` 取骨架，目录树/模糊找文件用 `fast_io_glob_tree`（均硬限 `<=8.8 KB` 零溢盘）。
- **Swarm 降级**：无 MCP 时改用并行 `read_file` 或真实 Python 调 `fast_io_mcp.py --cli <grep|outline|glob_tree|batch_read>`。

## 2. Windows PowerShell 5.1 护栏
- `run_command` 跑 **Windows PS 5.1**：**禁写 `&&` / `||`**，改用 `;` 或 `if ($?) { ... }`。
- **禁裸调缺失/占位命令**：不裸调 `grep`/`rg`/`fd`/`head`/`tail`/`wc`/`jq`/`which`/`realpath` 及 WindowsApps 空占位 `python`/`python3`；跑 Python 须用本机真实 Python 3.10+（如 `py -3` 或实际路径）。

## 3. 执行过程进度播报
- **干活时边做边说**：动手前用一句话说明要做什么；之后不用每步都说，有关键发现、切换方向、遇到卡点，或连续执行 4～5 步后，在同一回合附一句简短进度说明再继续调用工具，严禁全程沉默闷头执行。
- **同步原生进度条**：多步骤任务（3 步及以上）同步调用 `update_steps` / `update_focus` 刷新实时阶段状态。
