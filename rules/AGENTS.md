# Flow Pilot · Fast I/O 检索读写与极简交互规则（Windows PS 5.1）

## 1. 读写与检索路由（零溢盘 · Eager MCP）
- **常规/已知行段读文件（首选原生）**：单文件、小文件或已知行区间（`<=800` 行 / `46 KB`）**一律同回合并行调原生 `read_file`**（本地白名单 `~1s`、0 溢盘）。
- **Fast I/O Eager MCP（`ServerName="agy-flow-pilot_fast_io"`，全工具硬限 `<=8.8 KB` 零溢盘，直调时按需传 `toolSummary`）**：
  - `fast_io_grep(paths, pattern?|queries[<=15], output_mode?="matches"|"files_only"|"count", include_glob?, is_regex?, case_sensitive?, context_lines?)`：多词并发搜代码（自动跳过 `.gitignore`/二进制）。
  - `fast_io_outline(paths[<=30])`：`>800` 行大文件先取骨架再并行 `read_file` 精读（支持 py/ts/js/cs/go/rs/md/html/css/json/yaml/vue）。
  - `fast_io_glob_tree(root_path, pattern?, fuzzy?, max_depth?, max_entries?)`：目录树、通配符或 `fuzzy` 模糊找文件。
  - `fast_io_batch_read(items:[{path,start_line?,end_line?}]|paths:["p:s-e"])`：仅用于多文件小切片/头部探针（总计 `<=8.8 KB`）。
- **Swarm Worker 降级**：无插件 MCP 时改用并行 `read_file` 或本机 Python 调 `fast_io_mcp.py --cli <grep|outline|glob_tree|batch_read>`。

## 2. Windows PowerShell 5.1 命令规范
- `run_command` 运行于 **Windows PowerShell 5.1**：
  - **禁写 `&&` 或 `||`**（覆盖系统内置 `&&` 串联提示）；顺序执行用 `;` 或 `if ($?) { ... }`。
  - **禁裸调缺失/空占位命令**：不裸调 `grep`、`rg`、`fd`、`head`、`tail`、`wc`、`jq`、`which`、`realpath` 及 WindowsApps 空占位 `python` / `python3`；跑 Python 须用本机真实已安装的 Python 3.10+ 解释器绝对路径（跳过 `WindowsApps` 占位桩，可用 `py -3` 或按本机安装目录解析真实 `python.exe`）。

## 3. 安全精准编辑与极简回复
- **先核实再改**：调 `edit_file` 前已读最新上下文，确保 `Target` 文件内唯一匹配。
- **结论先行**：首句给结果并附 `[文件名](file:///...)` 与行号；禁贴大段已落盘代码、复述已知逻辑或寒暄，常规回复 `200–600` 字，不创建未要求的总结文档。
