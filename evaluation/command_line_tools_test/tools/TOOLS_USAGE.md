# `command_line_tools_test/tools` 命令行工具使用说明

本文档面向在本机跑通 HarmonyOS 工程构建、本地单测、模拟器、安装包与设备端 instrument 测试的自动化链路。**推荐对所有「工程根目录」「.hap 文件」使用绝对路径**，可避免工作目录变化导致的相对路径歧义。DevEco 路径建议通过环境变量 `DEVECO_PATH` 在 `.env` 中配置。

---

## 1. 运行方式与环境约定

### 1.1 工作目录（重要）

请在 **`command_line_tools_test` 目录** 下执行脚本，使 Python 能正确解析同目录下的 `_load_env` 模块：

```powershell
cd E:\WorkApp\MSWE-agent\MSWE-agent\MSWE-agent\command_line_tools_test
python tools\build_app.py --help
```

（Linux/macOS 将路径与反斜杠换成对应形式即可。）

### 1.2 `command_line_tools_test/.env`

每个工具的 `main()` 入口会调用 `ensure_command_line_tools_env()`：**以覆盖模式**加载 `command_line_tools_test/.env` 中的变量，并据此前置 `PATH`（如 `HDC_PATH`、`JAVA_HOME\bin`、`NODE_HOME`）。

建议在 `.env` 中至少配置（按本机实际路径填写）：

| 变量 | 作用 |
|------|------|
| `DEVECO_PATH` | DevEco Studio 安装根目录，用于定位 `hvigorw`、`ohpm`、SDK 推断等 |
| `DEVECO_SDK_HOME` / `OHOS_BASE_SDK_HOME` | OpenHarmony SDK；与工程 `local.properties` / hvigor 要求一致 |
| `JAVA_HOME` | 无全局 `java` 时用于 hvigor |
| `NODE_HOME` | 无全局 `node` 时用于 hvigor |
| `HDC_PATH` | 可选；若 `hdc` 不在 PATH，可指向 `hdc.exe` 所在目录或文件 |
| `EMULATOR_PATH` | 可选；模拟器启动程序或根目录（`ensure_emulator` / `start_emulator` 会用到） |
| `EMULATOR_DEPLOYED_PATH` | `start_emulator.py --list-instances` 需要 |

**说明：** 仓库根目录的 `.env` 里可能有 `REPO_PATH`，供 **Agent 修复流程** 使用。以下工具**不会**用 `REPO_PATH` 作为默认工程路径（避免与工具链 E2E 混淆）：

- `integration_test.py`
- `verify_tools.py` 的 `--deep` 模式  

其它单工具若需要工程路径，请显式传入 `--repo-path`。

### 1.3 日志与机器可读输出

多数工具在成功或失败时都会打印 **`LOG_PATH=...`**，日志写入：

`command_line_tools_test/dev_sessions/05_test/logs/`

文件名形如 `<工具名>-<时间戳>.log`。下游脚本或 Agent 可抓取 `LOG_PATH` 做故障分析。

部分工具还会打印 **`KEY=value`** 行（如 `BUILD_STATUS`、`HAP_PATH`、`TARGET`），便于管道解析。

---

## 2. 工具一览与参数说明

以下表格中「必填」指 argparse 层面 `required=True` 或未给则脚本直接报错；「可选」可省略并使用默认值或环境变量。

### 2.1 `build_app.py` — 使用 hvigor 构建 HAP

**用途：** 在工程根执行 `assembleHap`（或自定义 task），成功后输出相对**工程根**的 `HAP_PATH`，并写详细日志。

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--repo-path` | 是 | — | Harmony **工程根目录**（含 `hvigorw`、`build-profile.json5` 等），建议绝对路径 |
| `--deveco-path` | 否 | env `DEVECO_PATH` | DevEco Studio 安装目录，用于解析 `hvigorw`、`SDK`、`JBR/Node` |
| `--task` | 否 | `assembleHap` | hvigor 任务名 |
| `--mode` | 否 | `module` | hvigor `--mode` |
| `--module` | 否 | `entry` | `-p module=...` |
| `--product` | 否 | `default` | 产品名；也用于 SDK 与 `build-profile` 解析 |
| `--target` | 否 | 无 | 可选目标名（如 `ohosTest`） |
| `--find-only` | 否 | 关 | 不跑 hvigor，仅扫描仓库内**最新** `.hap` 并打印 SDK 解析信息 |

**典型命令：**

```powershell
python tools\build_app.py `
  --repo-path "E:\...\repair_repo\repo_after_fix\Media-Audio"
```

**成功时 stdout 要点：** `BUILD_STATUS=SUCCESS`、`HAP_PATH=<相对工程根>`、`LOG_PATH=...`  
若日志中出现 `BUILD SUCCESSFUL` 但进程退出码非 0，脚本会尽量根据日志判定成功并解析 HAP（与 `run_local_tests` 行为一致）。

---

### 2.2 `run_local_tests.py` — 本地单元测试（`src/test`，hvigor `test`）

**用途：** 默认在工程根执行 `ohpm install`（除非已能解析 `@ohos/hypium`），再调用 `hvigorw` 跑本地测试任务。

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--repo-path` | 是 | — | 工程根，建议绝对路径 |
| `--deveco-path` | 否 | env `DEVECO_PATH` | DevEco 安装目录 |
| `--mode` | 否 | `module` | hvigor `--mode` |
| `--module` | 否 | `entry` | 单模块跑测时使用 |
| `--product` | 否 | `default` | 产品名 |
| `--task` | 否 | `test` | 本地测试对应 hvigor 任务 |
| `--coverage` | 否 | 关 | 传入 `-p coverage=true`（需工程/插件支持） |
| `--prop` | 否 | 可重复 | 额外 `-p`，如 `--prop buildMode=debug` |
| `--all-local-modules` | 否 | 关 | 扫描所有含 `src/test/*.test.ets` 的模块并逐个执行 |
| `--discover-only` | 否 | 关 | 只打印发现的模块列表，不执行 hvigor |
| `--timeout-sec` | 否 | `0` | 单次 hvigor 超时秒数，`0` 表示不限制 |
| `--hvigor-stacktrace` | 否 | 关 | 追加 `--stacktrace` |
| `--hvigor-debug` | 否 | 关 | 追加 `--debug`，日志更详细 |
| `--skip-ohpm-install` | 否 | 关 | 跳过 `ohpm install`（可能导致依赖无法解析） |
| `--ohpm-timeout-sec` | 否 | `600` | `ohpm install` 超时 |

**典型命令：**

```powershell
python tools\run_local_tests.py `
  --repo-path "E:\...\Media-Audio" `
  --hvigor-stacktrace --hvigor-debug
```

**stdout 要点：** `LOCAL_TEST_STATUS=...`、`EXIT_CODE=...`、`LOG_PATH=...`；可能含 `REPORT_HINT=`（测试报告 HTML/目录线索）。  
日志文件路径与 `common.write_tool_log` 不同，为固定目录下的 `run_local_tests-<时间戳>.log`（仍在 `dev_sessions/05_test/logs` 下）。

---

### 2.3 `run_tests.py` — 发现测试目标并在设备上执行 instrument

**用途：**

- `--discover-only`：根据工程结构打印测试目标描述行（不做 hdc）。
- 默认：在 **恰好一个 online** 的 hdc 目标上执行 instrument 相关用例（不负责编译安装，需包已在设备上或流程前置完成）。

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--repo-path` | 是 | — | 工程根，建议绝对路径 |
| `--deveco-path` | 否 | env `DEVECO_PATH` | DevEco 安装目录 |
| `--timeout-sec` | 否 | 脚本内常量 | 单测命令最长等待时间（秒） |
| `--discover-only` | 否 | 关 | 仅发现目标 |
| `--product` | 否 | `default` | 读取 `build-profile.json5` 中 compile/compatible SDK 时用 |

**`--discover-only` 输出行格式（示例）：**

每条一行，由分号分隔字段，例如：

`kind=instrument;bundle=com.example.app;module=entry_test;source=entry/src/ohosTest;tests=3`

或 `kind=local;...`（本地测试目录的发现结果；完整执行时以 instrument 流程为主）。

**完整执行成功时 stdout：** `TEST_RUN_STATUS=COMPLETED`、`EXIT_CODE=...`、`LOG_PATH=...`

```powershell
python tools\run_tests.py `
  --repo-path "E:\...\Media-Audio" `
  --timeout-sec 1800
```

---

### 2.4 `install_app.py` — 通过 hdc 安装 `.hap`

**用途：** 将构建产物安装到 **online** 设备；可选打印工程 SDK 解析信息。

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--hap-path` | 是 | — | `.hap` 文件路径，**建议绝对路径** |
| `--target` | 否 | 自动 | hdc 连接串；省略时若仅有一个 online 目标则自动选用 |
| `--repo-path` | 否 | — | 若与 `--deveco-path` 同时提供，先打印 `build-profile` SDK 解析 |
| `--deveco-path` | 否 | — | 配合 `--repo-path` |

```powershell
python tools\install_app.py `
  --hap-path "E:\...\entry-default-unsigned.hap" `
  --repo-path "E:\...\Media-Audio"
```

**成功时：** `INSTALL_STATUS=SUCCESS`、`TARGET=...`、`HAP_PATH=...`（展示用相对路径）、`LOG_PATH=...`

---

### 2.5 `ensure_emulator.py` — 确保 hdc 有 online 模拟器/设备

**用途：** 等待或尝试拉起模拟器，直到 hdc 出现可用 target（失败抛错并退出码 1）。

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--emulator-path` | 否 | — | 模拟器根目录、实例目录或启动程序 |
| `--timeout-sec` | 否 | 脚本内默认 | 等待目标出现的超时（秒） |
| `--repo-path` | 否 | — | 与 `--deveco-path` 同用时打印 SDK 解析 |
| `--deveco-path` | 否 | — | 配合 `--repo-path` |

`integration_test.py` 会将 `EMULATOR_PATH` 环境变量或 `DevEco\tools\emulator\Emulator.exe` 作为 `--emulator-path` 传入。

**成功时：** `EMULATOR_STATUS=SUCCESS`、`TARGET=...`、`LOG_PATH=...`

---

### 2.6 `start_emulator.py` — 启动模拟器（依赖 `.env` 路径）

**用途：** 从 **`command_line_tools_test/.env`** 解析 `DEVECO_PATH`、`EMULATOR_*` 等并启动模拟器；**不要求** `--repo-path`。

| 参数 | 说明 |
|------|------|
| `--wait-hdc` | 启动后轮询 hdc 直至出现目标或超时 |
| `--wait-seconds` | 配合 `--wait-hdc`，默认 `180` |
| `--stop-stale` | Windows：无 target 时先 taskkill 旧模拟器进程 |
| `--show-command-only` | 只打印将要执行的命令 |
| `--list-instances` | 列出 `EMULATOR_DEPLOYED_PATH` 下实例名并退出 |

---

### 2.7 `extract_benchmark_patches.py` — 从 jsonl 抽取 patch 文件

**用途：** 从基准数据集中按 `repo` + `number` 匹配一行，写出 `test_patch` / `fix_patch` 到指定目录。**不涉及 Harmony 工程路径**，但路径参数仍建议用绝对路径。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--jsonl` | `<command_line_tools_test>/arkts_benchmark.jsonl` | jsonl 文件 |
| `--repo` | `TaskManagement-ReminderAgentManager` | 记录中的 `repo` 字段 |
| `--number` | `5926` | 记录中的 `number` 字段 |
| `--output-dir` | `command_line_tools_test` 根 | 输出目录 |
| `--test-output` | `test_patch.patch` | test patch 文件名 |
| `--fix-output` | `fix_patch.patch` | fix patch 文件名 |

```powershell
python tools\extract_benchmark_patches.py `
  --jsonl "E:\...\tests\arkts_benchmark.jsonl" `
  --repo "Media-Audio" `
  --number 5963 `
  --output-dir "E:\tmp\patches"
```

成功时打印 `test_patch=` / `fix_patch=` 绝对路径及 `LOG_PATH=...`。

---

### 2.8 `integration_test.py` — 端到端串联（真实子进程）

**用途：** 按顺序调用：`build_app` → `run_local_tests`（可跳过）→ `ensure_emulator`（可跳过）→ `install_app`（可跳过）→ `run_tests` → `extract_benchmark_patches`。最后打印 **JSON 报告** 与总 `LOG_PATH`。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--repo-path` | `<仓库根>/repair_repo/repo_after_fix/Media-Audio` | 工程根；**忽略** `.env` 的 `REPO_PATH` |
| `--deveco-path` | 环境变量 `DEVECO_PATH` | 未设置则报错退出 |
| `--jsonl` | `<仓库根>/tests/arkts_benchmark.jsonl` | 抽取 patch 用 |
| `--extract-repo` | `Media-Audio` | jsonl 的 `repo` 字段 |
| `--extract-number` | `5963` | jsonl 的 `number` |
| `--build-timeout-sec` | `900` | 构建超时 |
| `--run-tests-timeout-sec` | `1800` | instrument 全流程超时 |
| `--emulator-timeout-sec` | `120` | 等待模拟器 |
| `--skip-install` | 关 | 不安装 HAP（设备上无包时 instrument 可能失败） |
| `--skip-emulator` | 关 | 不跑 `ensure_emulator` |
| `--skip-local-tests` | 关 | 不跑 `run_local_tests`（默认**会**跑本地测试） |
| `--local-tests-timeout-sec` | `1800` | 本地测试超时 |
| `--local-debug` | 关 | 为 `run_local_tests` 加上 stacktrace/debug |

**退出码：** `build_app`、`run_local_tests`（未跳过）、`extract_benchmark_patches` 任一失败为 `1`；模拟器/安装/instrument 失败不单独把整体验证判死（便于无设备环境仍看构建与抽取结果）。

```powershell
python tools\integration_test.py `
  --repo-path "E:\...\repair_repo\repo_after_fix\Media-Audio" `
  --local-debug
```

---

### 2.9 `verify_tools.py` — 冒烟：`--help` + 可选深度 E2E

| 参数 | 说明 |
|------|------|
| （默认） | 对各脚本执行 `--help`，并测试 `common`/`_load_env` 导入 |
| `--deep` | 调用 `integration_test.py`；默认工程为 `<仓库根>/repair_repo/repo_after_fix/<subdir>` |
| `--repo-subdir` | 默认 `Media-Audio` |
| `--repo-fix-side` | `after_fix` 或 `before_fix`（映射到 `repo_after_fix` / `repo_before_fix`） |
| `--e2e-max-seconds` | 子进程墙钟上限，默认 `7200` |

**注意：** `--deep` 下默认仓库路径**不读取** `.env` 的 `REPO_PATH`，由 `--repo-fix-side` 与 `--repo-subdir` 决定。

---

### 2.10 `verify_repair_repos.py` — 批量对比 `repo_before_fix` / `repo_after_fix`

| 参数 | 说明 |
|------|------|
| `--repair-repo-root` | 默认 `<仓库根>/repair_repo` |
| `--deveco-path` | 默认环境变量 |
| `--skip-build` | 只做 SDK 预检 + `run_tests --discover-only` |
| `--build-timeout-sec` | 单次构建超时 |
| `--run-full-tests` | 额外跑完整 `run_tests`（需 hdc online） |
| `--test-timeout-sec` | 完整测试超时 |
| `--repo` | 只验证该文件夹名在 before/after 各一份 |

---

## 3. 推荐流水线（绝对路径示例）

以下占位符请换成本机路径：

1. **构建**

   `python tools\build_app.py --repo-path "<ABS_REPO>"`

2. **本地测试**（建议保留默认 `ohpm install`）

   `python tools\run_local_tests.py --repo-path "<ABS_REPO>"`

3. **设备准备**

   `python tools\ensure_emulator.py --emulator-path "<ABS_EMU_OR_EXE>" --repo-path "<ABS_REPO>"`

4. **安装**（`HAP_PATH` 可用上一步构建输出）

   `python tools\install_app.py --hap-path "<ABS_HAP>" --repo-path "<ABS_REPO>"`

5. **Instrument 测试**

   `python tools\run_tests.py --repo-path "<ABS_REPO>"`

6. **一键**（默认含本地测 + 模拟器 + 安装 + instrument + 抽取）

   `python tools\integration_test.py --repo-path "<ABS_REPO>"`

---

## 4. 常见问题

- **`Unable to find 'sdk.dir' / OHOS_BASE_SDK_HOME`**：在工程根配置 `local.properties` 的 `sdk.dir`，或确保 `.env` 中 SDK 相关变量与 DevEco 安装一致。  
- **`@ohos/hypium` 无法解析**：在工程根执行 `ohpm install`；`run_local_tests.py` 默认会尝试自动执行。  
- **多个 hdc online**：`install_app` / `run_tests` 在「多目标」场景可能要求显式 `--target` 或先 `hdc tmode` 只保留一个。  
- **路径含空格**：PowerShell 中用引号包裹整个路径参数。
- **无网络环境如何准备依赖**：参考 `command_line_tools_test/offline_env/README.md`，把 ohpm 的 store/cache 固定到仓库内并提前预热后整体拷贝。

---

## 5. 相关文件

- `BUILD_APP_USAGE.md`：若存在，可能包含针对 `build_app` 的补充说明。  
- `../README.md`：交付包总览（若与本文冲突，以**当前代码行为**与本文为准，例如 `.env` 已由工具自动加载并覆盖）。
