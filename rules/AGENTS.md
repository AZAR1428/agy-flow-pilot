# Flow Pilot · 读写路由与 Windows PS 5.1 规范

## 1. 读写与检索路由
- **常规/已知行段**：单文件或已知行区间（`<=800` 行 / `46 KB`）同回合并行调原生 `read_file`。
- **探索与大文件**：多词搜索用 `fast_io_grep`，`>800` 行大文件先用 `fast_io_outline` 取骨架，目录树/模糊找文件用 `fast_io_glob_tree`（均硬限 `<=8.8 KB` 零溢盘）。
- **Swarm 降级**：无 MCP 时改用并行 `read_file` 或真实 Python 调 `fast_io_mcp.py --cli <grep|outline|glob_tree|batch_read>`。

## 2. Windows PowerShell 5.1 护栏
- `run_command` 跑 **Windows PS 5.1**：**禁写 `&&` / `||`**，改用 `;` 或 `if ($?) { ... }`。
- **禁裸调缺失/占位命令**：不裸调 `grep`/`rg`/`fd`/`head`/`tail`/`wc`/`jq`/`which`/`realpath` 及 WindowsApps 空占位 `python`/`python3`；跑 Python 须用本机真实 Python 3.10+（如 `py -3` 或实际路径）。
