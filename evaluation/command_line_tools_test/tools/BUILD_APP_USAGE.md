# 编译 HAP 脚本使用说明

本文档说明如何使用 `tools/build_app.py` 在当前仓库中编译 HarmonyOS `.hap` 包。

## 作用

脚本负责完成以下事情：

- 接收仓库路径和 DevEco Studio 路径
- 定位 `hvigorw` / `hvigorw.bat`
- 尝试准备构建所需的 SDK / Node 环境变量
- 调用 hvigor 构建 HAP
- 查找最新生成的 `.hap` 文件
- 输出构建结果和 HAP 路径

当前脚本只覆盖“编译 HAP”这一步，不包含以下能力：

- 签名
- 证书 / profile / keystore 处理
- 模拟器启动
- 安装应用
- 测试执行

## 文件位置

- 脚本入口：`tools/build_app.py`
- 公共辅助：`tools/common.py`

## 前置条件

运行前请确认：

- 仓库中已有可用的 `build-profile.json5`
- 项目源码缺失文件已补齐，例如当前项目中的 `Encryption.ets`
- 已安装 DevEco Studio
- DevEco Studio 自带的 `hvigorw.bat` 可用，或传入路径下能定位到它
- Python 可用

注意：

- 脚本不会自动把 `build-profile.json5.bak` 重命名为 `build-profile.json5`
- 脚本不会补签名配置
- 如果某个 SDK 根不可用，脚本会自动尝试下一个候选 SDK 根

## 推荐用法

在仓库根目录执行：

```bash
python tools/build_app.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>"
```

这是当前仓库最直接的使用方式。

## 命令行参数

### 必填参数

- `--repo-path`
  - Harmony 工程目录
- `--deveco-path`
  - DevEco Studio 安装目录

### 可选参数

- `--task`
  - 默认：`assembleHap`
- `--mode`
  - 默认：`module`
- `--module`
  - 默认：`entry`
- `--product`
  - 默认：`default`
- `--target`
  - 默认不传
  - 可用于扩展到 `ohosTest` 等 target
- `--find-only`
  - 不执行构建，只扫描仓库内最新的 `.hap`

## 常见命令

### 1. 编译主应用 HAP

```bash
python tools/build_app.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>"
```

### 2. 查找最新 HAP，不重新编译

```bash
python tools/build_app.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>" --find-only
```

### 3. 指定 target 构建

```bash
python tools/build_app.py --repo-path ./repo/<project_name> --deveco-path "<deveco_path>" --task assembleHap --mode module --module entry --product default --target ohosTest
```

## 输出格式

### 成功时

```text
BUILD_STATUS=SUCCESS
HAP_PATH=<relative_hap_path>
```

### 失败时

```text
BUILD_STATUS=FAILED
<error message>
```

## Python 接口

脚本也可以作为 Python 模块直接调用。

### 1. 编译项目

```python
from tools.build_app import build_project

hap_path = build_project("./repo/<project_name>", r"<deveco_path>")
print(hap_path)
```

### 2. 查找最新 HAP

```python
from tools.build_app import find_built_hap

hap_path = find_built_hap("./repo/<project_name>")
print(hap_path)
```

## 当前项目的默认构建策略

`build_project(repo_path, deveco_path)` 当前默认执行的是：

```text
hvigorw(.bat) --no-daemon --mode module -p module=entry -p product=default assembleHap
```

如果后续项目 task 有变化，可以改用 CLI 参数覆盖。

## SDK 选择说明

脚本会尝试多个可能的 SDK 根，例如：

- 系统环境变量中的 `DEVECO_SDK_HOME`
- DevEco 本机配置里的 `oh.sdk.location`
- `--deveco-path` 相关目录
- DevEco 安装目录旁边常见的 SDK 目录

如果首个 SDK 根报这类错误：

```text
SDK component missing
Invalid value of 'DEVECO_SDK_HOME'
```

脚本会继续尝试下一个 SDK 根，而不是立即失败。

## 当前仓库上的验证结果

当前仓库已经验证可成功编译，产物示例为：

```text
repo\<project_name>\entry\build\default\outputs\default\entry-default-unsigned.hap
```

说明：

- 当前产物是未签名 HAP
- 这是符合本步骤范围的

## 常见失败问题

### 1. 缺少 `build-profile.json5`

表现：

```text
Missing required project file: <repo>\build-profile.json5
```

处理方式：

- 先确认工程是否已经补齐正式的 `build-profile.json5`

### 2. 找不到 `hvigorw.bat`

表现：

```text
Unable to find hvigorw/hvigorw.bat ...
```

处理方式：

- 检查 `--deveco-path` 是否传入 DevEco Studio 安装目录

### 3. SDK 组件不完整

表现：

```text
SDK component missing
```

处理方式：

- 先让脚本自动尝试其他 SDK 根
- 如果所有候选都失败，需要在 DevEco Studio 中补齐 SDK 组件

### 4. 构建成功但没找到 `.hap`

表现：

```text
No .hap file found under repo: ...
```

处理方式：

- 检查 task 是否正确
- 检查是否实际产出到了其他模块目录

## 建议调用顺序

如果后续自动化要复用，建议按下面顺序调用：

1. 先执行 `build_project(repo_path, deveco_path)`
2. 成功后记录返回的 HAP 路径
3. 如果只想复用上一次构建产物，再调用 `find_built_hap(repo_path)`

## 当前不处理的事项

以下事项应放到后续步骤，不在本脚本中实现：

- 签名
- 安装到模拟器或真机
- 启动应用
- 执行 `hdc`
- 执行测试
