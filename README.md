# Flow Pilot (`agy-flow-pilot` v3.8.0)

专为 **Google Antigravity** 打造的 **浅色统一胶囊桌面悬浮窗（Unified Capsule Telemetry HUD）+ Fast I/O 零溢盘本地文件引擎 + 断流自愈看门狗** 一体化插件套件。

---

## 一、架构总览（v3.8.0 · 原生重启恢复协同、Fast I/O 零溢盘 Eager 升级与零 RPC 上下文审计）

```text
%USERPROFILE%\.gemini\config\plugins\agy-flow-pilot\
├── plugin.json                # 插件清单与元信息 (v3.8.0，对齐 sidecar://agy-flow-pilot/pilot/ 自动唤起)
├── mcp_config.json            # 注册 fast_io 原生 Python Eager MCP 服务（45 字符工具名，per-tool eager）
├── hooks.json                 # SessionStart/PreInvocation 极速微标（无 PreToolUse 工具冷启动开销）
├── LICENSE                    # MIT 开源许可证
├── THIRD_PARTY_NOTICES.md     # 微软 WebView2 SDK 第三方组件许可声明
├── rules\
│   └── AGENTS.md              # 零溢盘读写路由（常规读走并行 read_file，探索走 Eager fast_io）与 PS 5.1 规范
├── assets\
│   └── logo.svg               # 暖白电光蓝科学遥测图标
├── scripts\
│   ├── hook_entry.py          # 零提示注入微标（基于父/根会话关联字段判定子代理）
│   └── fast_io_mcp.py         # 纯标准库 <=8.8KB 零溢盘 Eager MCP & --cli 引擎（多查询 grep/骨架/模糊树/微切片）
└── sidecars\
    └── pilot\
        ├── sidecar.json       # 注册 AUX_PANE / SIDEBAR 入口与 agent_permissions 声明
        ├── engine.py          # 16 路流连接池 (660s) + 服务端重启恢复宽限 + 零额外 RPC 上下文膨胀主因/路由模型审计
        ├── server.py          # 本地安全网关 + 单节点零 I/O 总线 + 桌面悬浮窗进程监管器与生命周期审计
        ├── ui.html            # 浅色统一胶囊双模前端（卡片缓存悬崖提示、膨胀主因抽屉、诚实中断按钮、无障碍降级）
        └── host\
            ├── FlowPilotHost.cs
            ├── FlowPilotHost.exe              # 光标所在活跃屏定位 / 防误关 / 息屏自愈 / 八方向缩放无边框置顶宿主
            ├── Microsoft.Web.WebView2.Core.dll
            └── WebView2Loader.dll
```

> 仓库内还包含开发维护专用脚本 `scripts/build_host.ps1`（安全编译 `FlowPilotHost.exe`）与 `scripts/pre_publish_audit.py`（发布前安全脱敏审计），`install.ps1` 按白名单部署时不会将其拷贝到插件运行目录。

---

## 二、v3.8.0 更新亮点

1. **Google Antigravity 运行机制适配与竞态消除**：
   - **诚实单按钮中断操作**：移除已废弃的后台转交调用，将 HUD 挂起行中冗余按钮合并为诚实的单按钮 `⏹ 中断步骤`（保留 `⚡ 唤醒` 与后台脚本 `终止后台`）。
   - **协同客户端原生重启恢复**：看门狗自动续跑豁免 `server shutdown` / `server restart` 引起的步骤中断，并在引擎启动或本地语言服务实例重绑后设置 `30s` 静默宽限期，避免与客户端原生重启恢复双发 `继续`。
   - **Hook 子代理判定与面板导航对齐**：保留 `SessionStart` + `PreInvocation`（及首轮后零 I/O 快退与时间戳记录），清理无效字段并改用原生父/根会话关联字段判定子代理；优化 `plugin.json` 文案以配合 Google Antigravity 插件面板导航产出 `[Flow Pilot](sidecar://agy-flow-pilot/pilot/)` 一键开窗胶囊。
2. **Fast I/O v3.8.0：`<=8.8 KB` 零溢盘、官方 Eager 注册与多查询/骨架/CLI 全面升级**：
   - **全工具 `<= 8,800 B` UTF-8 硬上限 + 水桶公平分配**：`fast_io_batch_read`、`fast_io_grep`、`fast_io_glob_tree`、`fast_io_outline` 统一严守 `8.8 KB` 内联上限，彻底消除 Antigravity `~10 KB` MCP 溢盘落盘；采用注水公平预算分配，并在截断尾部输出精确的 `read_file Lx-Ly` 续读提示。
   - **官方 Eager MCP 与 `readOnlyHint` 元数据**：`mcp_config.json` 服务键名精简为 `fast_io`（工具全名仅 `45` 字符，远离 `64` 字符上限），按官方文档逐工具声明 `"eager": true` 省去冷启动读 Schema 轮次；所有工具补齐标准只读 `annotations`（`readOnlyHint: true`, `destructiveHint: false`, `openWorldHint: false`）。
   - **重写 `rules/AGENTS.md` 路由**：常规单文件/小文件与已知行段（`<=800` 行 / `46 KB`）一律改走同回合并行原生 `read_file`（本地白名单 `~1s`、0 溢盘）；Eager `fast_io` 专注多词检索、大文件骨架、目录模糊定位与小切片探针，并完整保留 Windows PS 5.1 规范。
   - **`fast_io_grep` 多查询并发与公平覆盖**：新增 `queries[]`（单次最多 `15` 条并发扫描，完全兼容单 `pattern`）与 `output_mode`（`matches` / `files_only` / `count`）；两阶段预算保证每个命中文件先拿首条匹配，再由剩余预算水桶分配扩展其余命中。
   - **`fast_io_outline` 顶层符号优先与多格式扩展**：单次文件上限从 `15` 提至 `30`，在 `8.8 KB` 内优先保障全部文件的顶层符号再填充嵌套成员；新增纯标准库 `.json`（顶层键）、`.yaml`/`.yml`（顶层键）与 `.vue`（`<template>`/`<script>`/`<style>` 及脚本导出/函数）提取器。
   - **Swarm `--cli` 直调、模糊找文件与安全护栏**：新增 `fast_io_mcp.py --cli <batch_read|grep|glob_tree|outline>` 赋能无 MCP 挂载的 Swarm Worker；`fast_io_glob_tree` 新增 `fuzzy` 子序列模糊路径排序；增加 `4 MB` stdio 帧上限并拒绝直接扫描裸盘符（`C:\`、`/`）或用户家目录（`~`）。
3. **零额外 RPC 上下文审计、缓存悬崖外露与 HUD 细节打磨**：
   - **外层卡片缓存悬崖指示**：当检测到连续缓存未命中或大上下文冷启动时，卡片 `.usage-chip` 立即高亮琥珀色并附加 `↓` 提示，不加宽且在 `320px` 窄屏零换行。
   - **上下文抽屉膨胀主因与路由模型**：直接解析流帧已下发的 `contextWindowUsage.estimatedAfterCompaction`（显示 `(压缩后预估)`）、`tokenGroups[].children`（展示 Top 膨胀主因）及 `responseModel` 实际路由模型（`实际路由：...`），零额外 RPC 开销。
   - **子代理静息水位、活跃屏定位与动效降级**：子代理 `.sub-meter` 非流式时展示上下文占比进度条；支持 `@media (prefers-reduced-motion: reduce)` 动效降级；`FlowPilotHost.exe` 首次打开与重置位置时通过 `GetCursorPos` + `MonitorFromPoint` 精准定位至鼠标所在的当前活跃显示器。

---

## 三、安装与部署说明（`install.ps1`）

在 Windows PowerShell 中执行 `.\install.ps1` 即可安装或热更新至 `%USERPROFILE%\.gemini\config\plugins\agy-flow-pilot\`：

- **白名单精简部署**：仅按显式白名单拷贝运行时必需文件与目录（`plugin.json`、`mcp_config.json`、`hooks.json`、`LICENSE`、`THIRD_PARTY_NOTICES.md`、`assets/`、`rules/`、`scripts/fast_io_mcp.py`、`scripts/hook_entry.py`、`sidecars/`），并自动过滤 `__pycache__`、`*.pdb`、`*.tmp`、`*.log` 及 WebView2 用户数据目录，绝不将开发脚本或临时文件带入插件运行目录。
- **平滑热替换与旧配置清理**：覆盖前自动停止旧版 `FlowPilotHost.exe` 与 sidecar Python 进程，自动探测并绑定真实 Python 3.10+ 解释器（跳过 WindowsApps 空占位），迁移 `fast_io` Eager MCP 配置；同时仅清理旧版本可能遗留在 `~/.gemini/config/hooks.json` 中的 `flow-pilot-swarm-wait-guard` 条目（若文件已空则移除空文件）。
- **零侵入原则**：`install.ps1` **严禁且绝不会修改** `~/.gemini/config/config.json`。

### 可选优化：为 Fast I/O 添加只读 MCP 放行规则（完全用户自愿）

> Flow Pilot **严禁且绝不会自动修改** `~/.gemini/config/config.json`。

在 Antigravity 默认 `Auto` 权限模式下，只读 Fast I/O MCP 工具已声明 `readOnlyHint: true` 可自动放行，但每次调用仍会经过远端安全策略检查。若您希望彻底跳过只读 Fast I/O 工具的远端审查等待，可自行在 `~/.gemini/config/config.json`（或 Antigravity 权限设置面板）的 Allow 规则列表中**手动**加入以下条目：

```json
"mcp(agy-flow-pilot_fast_io/*)"
```

---

## 四、桌面悬浮窗防消失加固与生命周期审计

- **防意外关闭守卫（Anti-Accidental-Close Guard）**：原生宿主在 `WndProc` / `OnFormClosing` 中拦截非授权的 `Alt+F4`、`WM_CLOSE`、`SC_MINIMIZE` 与 `SC_MAXIMIZE`，仅放行显式关闭/收起指令或系统关机，避免快捷键或窗口管理误触导致悬浮窗消失。
- **多屏活跃定位 / 休眠唤醒 / TopMost 自动恢复**：首次打开与重置位置自动对齐鼠标所在活跃屏；监听显示拓扑变更（`WM_DISPLAYCHANGE`）、休眠唤醒（`WM_POWERBROADCAST`）、DWM/DPI 与工作区变更事件，结合 `8s` 零抢焦点轻量可见性守卫自动将窗口拉回有效工作区并保持置顶。
- **坐标防污染与布局重排守卫**：前端 `ui.html` 对宿主回传的 `x`/`y` 执行有限非哨兵区间校验（`(-10000, 30000)`），防止系统最小化 `-32000` 哨兵坐标写入配置；`HostWindowBridge.resize` 仅在桥接发送成功或降级 DOM 缩放时提交去重缓存。
- **全链路 `hud_lifecycle.log` 审计轨迹**：`server.py` 与 `FlowPilotHost.exe` 统一写入线程安全、上限 `256KB`（自动裁剪保留尾部 `~128KB`）的 `<PLUGIN_DATA>/hud_lifecycle.log`，完整追溯启动、退出、来源与自愈事件。

---

## 五、核心特性

1. **统一单壳等宽胶囊 UI 与零抖动交互（Unified Light Capsule & Anti-Jitter）**：
   - 单外框无缝连体与三段等距顶栏，展开/收起保持完全等宽（默认 `388px`，支持 `320–960px` 左右缩放与 `lockTop` 零漂移）；默认 `36px` 胶囊无闪现启动，空闲时自动确定性收起；拖拽实时工作区边界钳位，支持无障碍减弱动效降级。
2. **`/boost` & `/swarm` 全阶段 `tok/s` 测速与全口径上下文/缓存遥测**：
   - 按模型隔离的思考/回复 EMA 校准先验持久化至 `<PLUGIN_DATA>/model_calibration.json`，新拉起的子代理首轮思考即输出准确 `tok/s`；纯内存流帧幂等聚合与重试去重，支持卡片级缓存悬崖告警与抽屉级 `tokenGroups` 膨胀主因透视。
3. **动态模型上下文同步与通用基线回退**：
   - 优先通过本地服务实时同步 `maxContextTokens` 与模型展示名 `displayName`，未命中时平滑回退至通用上下文基线，确保主会话、子代理与 Swarm 节点上下文占比一致。
4. **极致轻量化空闲低开销运行与安全自停止热升级**：
   - `sinceRev` 单调修订号短路协议（静默态 `<0.2ms` 返回 `<48B`）+ `660s` 长思考连接池 + 有界只读 SQLite 感知；`install.ps1` 按白名单安全覆盖安装并由客户端自动拉起新版实例。

---

## 六、开源发布前安全脱敏自检规范（防再犯检查清单）

为防止后续迭代误将凭据、本机路径、调试符号或平台内部信息带入公开仓库，每次提交发布前必须严格执行以下 4 项检查：

1. **Git 隐私邮箱核验**：
   - 确认本地与全局 `git config user.email` 以及近期提交历史均使用 GitHub 隐私邮箱（`*@users.noreply.github.com`），严禁暴露个人或企业真实邮箱。
2. **严禁硬编码敏感与平台内部信息**：
   - 严禁在代码、注释、文档或提交信息中硬编码任何 API Key / Token / 私钥、本机用户路径或用户名、个人会话 UUID、未公开内部模型占位 ID（`MODEL_PLACEHOLDER_M*` 格式）与内部代号、以及未公开发布的客户端版本号；模型名称与上下文容量一律通过本地接口动态获取。
3. **原生宿主 `.exe` 必须经由 `scripts/build_host.ps1` 构建**：
   - 重新编译 `sidecars/pilot/host/FlowPilotHost.exe` 时一律执行 `.\scripts\build_host.ps1`，脚本强制启用 `/target:winexe /platform:x64 /optimize+ /debug-`，并在编译后自动校验 PE `IMAGE_DEBUG_DIRECTORY` 为零且无 `RSDS` / `.pdb` / `Users\` 路径残留；严禁提交本地调试构建产物或 `.pdb`。
4. **每次 `git push` / 发版前必跑 `scripts/pre_publish_audit.py`**：
   ```powershell
   python -S -B scripts/pre_publish_audit.py
   ```
   - 零第三方依赖，自动覆盖凭据与高熵字面量、本机路径/邮箱/内网 IP/UUID、平台内部占位模式、PE 二进制隐私与近期 Git 提交邮箱校验；维护者还可在仓库根目录创建已被 `.gitignore` 忽略的 `.audit-denylist.local`（每行一条关键词或 `re:<正则>`）扩展私有保密词表。**必须确认输出 `PASS: 0 findings.`（退出码 `0`）后再执行推送或打 Tag。**

---

## 七、免责声明（非官方本地接口）

Flow Pilot 为独立开源社区插件，与 Google 或 Antigravity 官方团队无任何隶属或背书关系。本插件仅在本机回环地址（`127.0.0.1`）读取本地运行态遥测与会话状态以提供可视化悬浮窗与只读文件加速功能，不会向任何外部服务器上传用户代码、对话内容或凭据。由于部分遥测能力依赖非官方公开的本地运行时接口，其行为可能随 Google Antigravity 版本演进而变更，请按需自行评估使用风险。

---

## 八、开源许可与第三方组件声明

- 本项目源代码采用 **MIT License** 开源，详见根目录 [`LICENSE`](LICENSE)。
- 仓库随附的微软 WebView2 SDK 运行时组件（`sidecars/pilot/host/Microsoft.Web.WebView2.Core.dll` 与 `sidecars/pilot/host/WebView2Loader.dll`）为微软官方签名分发组件，遵循 **Microsoft WebView2 SDK License**，完整归属与许可信息详见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
