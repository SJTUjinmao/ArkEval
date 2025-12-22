# ArkTS 自动修复工具

一个基于 Pangu 大语言模型的智能代码修复工具，能够自动定位问题、生成补丁并应用到代码库中。本工具专为 HarmonyOS ArkTS 项目设计。

## ⚡ 一键启动

```bash
python3 run_full_workflow.py \
    --repo repos/Homogram \
    --problem "修复 HomeTopSearch 与 ChatList 重叠的问题" \
    --pangu-model-path /opt/pangu/openPangu-Embedded-7B-V1.1 \
    --apply
```

## 🎯 核心特性

- ✅ **智能函数定位**：基于语义相似度搜索和 LLM 筛选
- ✅ **自动补丁生成**：使用 Pangu 大语言模型生成高质量的代码修复
- ✅ **安全补丁应用**：支持 git apply 和代码块替换两种模式
- ✅ **完整工作流**：一键完成从问题描述到代码修复的全流程

## 🚀 快速开始

### 1. 环境要求

- **Python**: 3.9 或更高版本
- **Git**: 2.0 或更高版本
- **Pangu 模型**: 已下载到 `/opt/pangu/openPangu-Embedded-7B-V1.1`

1. **函数定位器 (Function Locator)**：在代码库中智能定位与问题描述相关的函数和文件
2. **补丁生成器 (Patch Generator)**：基于定位结果，使用 LLM 生成代码修复补丁
3. **补丁执行器 (Patch Executor)**：将生成的补丁安全地应用到代码库中

```bash
cd /root/PanGUfixerplus/arkts-auto-fix
pip install -r requirements.txt
```

### 3. 一键运行

```bash
# 方式一：直接提供问题描述
./run.sh "修复 HomeTopSearch 与 ChatList 重叠的问题"

# 方式二：从文件读取问题描述
./run.sh --file example_problem.txt
```

就这么简单！脚本会自动完成：
1. 清理缓存和恢复仓库状态
2. 定位相关函数
3. 生成补丁
4. 应用补丁到代码库

## 📖 详细使用说明

### 基本用法

```bash
# 使用默认配置运行
./run.sh "问题描述"

# 从文件读取问题描述
./run.sh --file example_problem.txt
```

### 使用 Python 脚本（更多控制）

```bash
# 仅生成补丁，不应用
python3 run_full_workflow.py \
    --repo repos/Homogram \
    --problem "修复某个bug" \
    --pangu-model-path /opt/pangu/openPangu-Embedded-7B-V1.1

# 生成并应用补丁
python3 run_full_workflow.py \
    --repo repos/Homogram \
    --problem "修复某个bug" \
    --pangu-model-path /opt/pangu/openPangu-Embedded-7B-V1.1 \
    --apply

# 指定输出目录
python3 run_full_workflow.py \
    --repo repos/Homogram \
    --problem "修复某个bug" \
    --pangu-model-path /opt/pangu/openPangu-Embedded-7B-V1.1 \
    --output my_output \
    --apply
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--repo` | 仓库路径（必需） | - |
| `--problem` | 问题描述（必需） | - |
| `--pangu-model-path` | Pangu 模型路径 | `/opt/pangu/openPangu-Embedded-7B-V1.1` |
| `--output` | 输出目录 | `test_output` |
| `--apply` | 应用补丁 | 默认不应用 |
| `--no-verify` | 不验证补丁 | 默认验证 |

## 🏗️ 系统架构

### 完整工作流程

```
问题描述
   │
   ▼
┌─────────────────┐      ┌──────────────────┐      ┌─────────────────┐
│ Function Locator│ ───> │ Patch Generator │ ───> │ Patch Executor  │
│   (函数定位)      │      │   (补丁生成)     │      │   (补丁应用)    │
│                  │      │                  │      │                  │
│ • 扫描代码库      │      │ • 读取定位结果    │      │ • 读取补丁文件    │
│ • 生成文件摘要    │      │ • 调用 Pangu 生成 │      │ • 应用补丁       │
│ • 相似度搜索      │      │ • 格式化补丁      │      │ • 验证结果       │
│ • 提取函数        │      │ • 保存补丁文件    │      │ • 创建临时分支   │
│ • LLM 筛选        │      │                  │      │                  │
└─────────────────┘      └──────────────────┘      └─────────────────┘
   │                          │                          │
   ▼                          ▼                          ▼
locator_output.json      all_patches_*.json         修改后的代码文件
                        all_patches_*.diff
```

### 模块说明

#### 1. Function Locator（函数定位器）

**位置**：`function_locator/`

**功能**：
- 扫描代码库，查找所有 `.ets` 文件
- 为每个文件生成功能摘要（使用 Pangu 模型）
- 使用嵌入模型计算问题描述与文件摘要的相似度
- 提取匹配文件中的函数
- 使用 LLM 筛选出最相关的目标函数

**输出**：`test_output/locator_output.json`

#### 2. Patch Generator（补丁生成器）

**位置**：`patch_generator/`

**功能**：
- 读取函数定位结果
- 提取目标函数代码和上下文
- 调用 Pangu 模型生成修复后的代码
- 格式化补丁为 unified diff 格式
- 保存补丁文件（JSON + DIFF）

**输出**：`test_output/patches/all_patches_*.json` 和 `all_patches_*.diff`

#### 3. Patch Executor（补丁执行器）

**位置**：`patch_executor/`

**功能**：
- 读取补丁文件
- 优先使用 `git apply` 应用补丁
- 如果失败，回退到代码块替换模式
- 验证补丁应用结果
- 创建临时分支保存更改

**输出**：修改后的代码文件

## ⚙️ 配置说明

### 环境变量

```bash
# 设置输出目录
export FUNCTION_LOCATOR_OUTPUT_DIR="/path/to/output"

# 设置 Top-K 文件数量（相似度搜索返回的文件数）
export FUNCTION_LOCATOR_TOP_K=10

# 设置模型缓存目录
export FUNCTION_LOCATOR_MODEL_CACHE_DIR="/home/dataset/xiebang"
```

### 修改默认配置

编辑 `run.sh` 文件中的默认配置：

```bash
REPO_PATH="${SCRIPT_DIR}/repos/Homogram"
PANGU_MODEL_PATH="/opt/pangu/openPangu-Embedded-7B-V1.1"
OUTPUT_DIR="test_output"
```

## 📊 输出结果

### 文件结构

```
test_output/
├── locator_output.json          # 函数定位结果
├── patches/                      # 补丁文件目录
│   ├── all_patches_*.json      # 所有补丁（JSON 格式）
│   └── all_patches_*.diff      # 所有补丁（Unified Diff 格式）
├── summaries/                   # 文件摘要缓存
│   └── *.json
├── embeddings_cache/            # 嵌入向量缓存
│   └── *.json
└── workflow.log                 # 工作流日志
```

### 查看结果

```bash
# 查看定位结果
cat test_output/locator_output.json | python3 -m json.tool

# 查看补丁文件
cat test_output/patches/all_patches_*.json | python3 -m json.tool

# 查看 diff 格式补丁
cat test_output/patches/all_patches_*.diff

# 查看修改的文件
cd repos/Homogram
git status --short
```

## ❓ 常见问题

### Q1: 模型路径不存在

**问题**：`模型路径不存在: /opt/pangu/openPangu-Embedded-7B-V1.1`

**解决**：
1. 确认 Pangu 模型已下载到指定路径
2. 或使用 `--pangu-model-path` 参数指定正确的路径

### Q2: 补丁应用失败

**问题**：`Git apply failed` 或 `Could not locate code block to replace`

**解决**：
1. 检查代码库是否有未提交的更改
2. 确保目标文件存在且路径正确
3. 查看详细日志：`tail -f test_output/workflow.log`
4. 系统会自动回退到代码块替换模式

### Q3: 定位结果不准确

**问题**：找到的函数与问题描述不相关

**解决**：
1. 增加 `FUNCTION_LOCATOR_TOP_K` 环境变量，扩大搜索范围
2. 优化问题描述，使用更具体的关键词
3. 检查文件摘要质量，必要时重新生成

### Q4: 如何撤销更改

**解决**：
```bash
# 恢复仓库到干净状态
cd repos/Homogram
git reset --hard HEAD
git clean -fd

# 或重新运行脚本（会自动重置）
./run.sh "新问题描述"
```

### Q5: 使用 conda 环境

如果使用 conda 环境（如 pangu 环境），脚本会自动检测并激活：

```bash
# 手动激活环境（可选）
conda activate pangu

# 然后运行脚本
./run.sh "问题描述"
```

## 🛠️ 开发指南

### 项目结构

```
arkts-auto-fix/
├── function_locator/          # 函数定位模块
│   ├── __init__.py
│   ├── config.py              # 配置文件
│   ├── locator.py             # 主定位器
│   ├── file_scanner.py        # 文件扫描器
│   ├── file_summarizer.py     # 文件摘要生成器
│   ├── embedder.py            # 嵌入向量生成器
│   ├── similarity_search.py   # 相似度搜索器
│   ├── function_extractor.py  # 函数提取器
│   └── llm_filter.py          # LLM 过滤器
├── patch_generator/           # 补丁生成模块
│   ├── __init__.py
│   ├── generator.py            # 主生成器
│   ├── llm_patch.py           # LLM 补丁生成
│   └── patch_formatter.py     # 补丁格式化
├── patch_executor/             # 补丁执行模块
│   ├── __init__.py
│   ├── executor.py            # 主执行器
│   ├── patch_applier.py       # 补丁应用器
│   └── verifier.py            # 补丁验证器
├── run.sh                      # 一键启动脚本
├── run_full_workflow.py        # Python 工作流脚本
├── pangu_model.py              # Pangu 模型封装
├── example_problem.txt         # 示例问题文件
├── requirements.txt            # Python 依赖
└── README.md                   # 本文档
```

### 单独测试各模块

```bash
# 测试函数定位器
python3 -c "
from function_locator import FunctionLocator
from pathlib import Path
locator = FunctionLocator(
    pangu_model_path='/opt/pangu/openPangu-Embedded-7B-V1.1',
    output_dir=Path('test_output')
)
result = locator.locate(Path('repos/Homogram'), '测试问题')
print(result)
"

# 测试补丁生成器
python3 -c "
from patch_generator import PatchGenerator
from pathlib import Path
generator = PatchGenerator(
    pangu_model_path='/opt/pangu/openPangu-Embedded-7B-V1.1',
    output_dir=Path('test_output/patches')
)
# 需要先有 locator_output.json
"

# 测试补丁执行器
python3 -c "
from patch_executor import PatchExecutor
from pathlib import Path
executor = PatchExecutor(
    repo_path=Path('repos/Homogram'),
    patches_dir=Path('test_output/patches')
)
result = executor.execute()
print(result)
"
```

## 📝 使用示例

### 示例 1：修复布局问题

```bash
./run.sh "修复 HomeTopSearch 与 ChatList 重叠的问题"
```

### 示例 2：添加新功能

```bash
./run.sh "在 PhoneNumber.ets 中添加 Lottie 动画，使用远程 JSON 资源"
```

### 示例 3：从文件读取问题

```bash
# 编辑问题描述文件
cat > my_problem.txt << EOF
在 ChatDetailBottom.ets 中添加 Lottie 动画组件，
使用远程 JSON 资源，实现自动播放和循环。
EOF

# 运行
./run.sh --file my_problem.txt
```

## 📚 相关文档

- **Pangu 模型**：使用 `/opt/pangu/openPangu-Embedded-7B-V1.1` 路径下的模型
- **配置文件**：`function_locator/config.py` 包含完整配置项说明

## 📝 许可证

本项目仅供学习和研究使用。

---

**最后更新**：2025-12-01  
**版本**：2.0.0
