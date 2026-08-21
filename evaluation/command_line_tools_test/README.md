# HarmonyOS 自动化工具交付说明

这份交付包包含可直接复用的 HarmonyOS 自动化脚本、示例会话记录和构建缓存说明，目标是让接收方在自己的机器上补齐本机参数后即可继续构建、拉起模拟器、安装应用和执行测试。

仓库内的 Harmony 工程统一约定放在 `repo/` 目录下，文档示例统一写成 `repo/<project_name>`。

## 目录说明

- `.hvigor/`
  - 当前工程的 hvigor 相关产物与日志。
- `tools/`
  - 自动化脚本入口。
  - `build_app.py`：构建 `.hap`
  - `ensure_emulator.py`：确保模拟器 target 在线
  - `install_app.py`：安装 `.hap`
  - `run_tests.py`：发现并执行测试
- `dev_sessions/`
  - 每一步实现和验证过程的说明文档、日志、截图。
  - 其中的路径已经整理成相对路径或参数占位符，方便跨机器复用。
- `.env`
  - 参数模板，不会被脚本自动加载。

## 环境要求

- Windows
- Python 3
- 已安装 DevEco Studio
- `hdc` 已加入系统 `PATH`
- 如需自动拉起模拟器，需要本机存在可用的 Harmony 模拟器实例

## 路径约定

- 仓库内路径统一用相对路径，例如：
  - `./repo/<project_name>`
  - `./repo/<project_name>/entry/build/default/outputs/default/entry-default-unsigned.hap`
- 机器相关的外部路径不要写死在仓库里，统一作为参数传入：
  - `--deveco-path "<deveco_path>"`
  - `--emulator-path "<emulator_path>"`

## 快速开始

1. 按本机环境填写 `.env` 中的占位参数。
2. 确认命令行里可直接执行 `hdc`.
3. 在仓库根目录执行下面的命令。

构建：

```bash
python tools/build_app.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>"
```

启动或复用模拟器：

```bash
python tools/ensure_emulator.py --emulator-path "<emulator_path>" --timeout-sec 150
```

安装应用：

```bash
python tools/install_app.py --hap-path ./repo/<project_name>/entry/build/default/outputs/default/entry-default-unsigned.hap --target 127.0.0.1:5555
```

执行测试：

```bash
python tools/run_tests.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>" --timeout-sec 120
```

## 输出说明

- 构建成功时，`build_app.py` 会输出相对形式的 `HAP_PATH`
- 安装成功时，`install_app.py` 会输出相对形式的 `HAP_PATH`
- 测试执行日志会写到 `dev_sessions/05_test/logs/`
- `run_tests.py` 返回的 `LOG_PATH` 也是相对路径

## 补充说明

- 当前 `.env` 只是交付备注模板，不是脚本配置源。
- `dev_sessions/` 中保留了示例命令、验证结论和截图，适合接手人快速理解链路。
- 如果接收方的 DevEco Studio、模拟器或 SDK 安装位置不同，只需要替换命令参数，不需要修改脚本源码。
