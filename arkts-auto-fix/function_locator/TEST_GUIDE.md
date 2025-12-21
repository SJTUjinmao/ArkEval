# Function Locator 测试指南

本指南介绍如何运行和测试 `function_locator` 功能。

## 📋 前置条件

### 1. 确保 Ollama 服务运行

```bash
# 检查 Ollama 服务是否运行
curl http://localhost:11500/api/tags

# 如果没有运行，启动 Ollama 服务
ollama serve
```

### 2. 确保模型已下载

```bash
# 下载嵌入模型
ollama pull qwen3-embedding:8b

# 下载 LLM 模型
ollama pull qwen3-coder:30b
```

### 3. 检查 Python 依赖

```bash
# 安装依赖（如果需要）
pip install requests numpy faiss-cpu  # 或其他索引库
```

## 🚀 运行方式

### 方式 1: 使用命令行脚本（推荐）

```bash
# 基本用法
python scripts/run_function_locator.py \
    --repo_path /path/to/repo \
    --problem "修复登录功能的验证逻辑"

# 从文件读取问题描述
python scripts/run_function_locator.py \
    --repo_path /path/to/repo \
    --problem_file problem.txt

# 指定输出文件
python scripts/run_function_locator.py \
    --repo_path /path/to/repo \
    --problem "问题描述" \
    --output my_output.json

# 自定义 Ollama 配置
python scripts/run_function_locator.py \
    --repo_path /path/to/repo \
    --problem "问题描述" \
    --ollama_host http://localhost:11500 \
    --embedding_model qwen3-embedding:8b \
    --llm_model qwen3-coder:30b

# 指定 top-k 文件数量
python scripts/run_function_locator.py \
    --repo_path /path/to/repo \
    --problem "问题描述" \
    --top_k 10
```

### 方式 2: 在 Python 代码中使用

```python
from function_locator import FunctionLocator, Config
from pathlib import Path

# 初始化定位器
locator = FunctionLocator(
    ollama_host="http://localhost:11500",
    embedding_model="qwen3-embedding:8b",
    llm_model="qwen3-coder:30b"
)

# 执行定位
result = locator.locate(
    repo_path=Path("./repos/Homogram"),
    problem_statement="修复登录功能的验证逻辑"
)

# 查看结果
print(f"目标函数: {result.target_function.function_name}")
print(f"文件: {result.target_function.file_path}")
print(f"行号: {result.target_function.start_line}-{result.target_function.end_line}")
print(f"代码: {result.code_before[:200]}...")
print(f"匹配文件数: {len(result.matched_files)}")
print(f"候选函数数: {len(result.candidate_functions)}")

# 保存结果
locator.output_writer.write(result, "locator_output.json")
```

### 方式 3: 使用便捷函数

```python
from function_locator import locate_functions, save_results_for_patch

# 查找函数
result = locate_functions(
    problem_statement="修复登录功能的验证逻辑",
    repo_path="./repos/Homogram",
    ollama_host="http://localhost:11500"
)

# 保存结果
output_path = save_results_for_patch(result, "output.json")
print(f"结果已保存到: {output_path}")
```

## 🧪 测试示例

### 示例 1: 测试 Homogram 项目

```bash
python scripts/run_function_locator.py \
    --repo_path /home/xiebang/HUAWEI\ 200W/repos/Homogram \
    --problem "修复消息气泡组件的显示问题" \
    --output homogram_test.json
```

### 示例 2: 测试特定功能定位

```bash
python scripts/run_function_locator.py \
    --repo_path /home/xiebang/HUAWEI\ 200W/repos/Homogram \
    --problem "如何实现用户认证功能？" \
    --top_k 10 \
    --output auth_test.json
```

### 示例 3: 从文件读取问题

```bash
# 创建问题文件
echo "修复定时器清理逻辑，确保在组件销毁时正确释放资源" > problem.txt

# 运行定位
python scripts/run_function_locator.py \
    --repo_path /home/xiebang/HUAWEI\ 200W/repos/Homogram \
    --problem_file problem.txt \
    --output timer_fix.json
```

## 📊 输出结果说明

运行成功后，会生成 JSON 输出文件，包含以下信息：

```json
{
  "problem_statement": "修复登录功能的验证逻辑",
  "target_function": {
    "function_name": "validateLogin",
    "file_path": "src/main/ets/views/Login.ets",
    "start_line": 45,
    "end_line": 78,
    "code": "function validateLogin(...) { ... }"
  },
  "reasoning": "LLM rerank score: 0.95",
  "code_before": "原始函数代码...",
  "matched_files": [
    "src/main/ets/views/Login.ets",
    "src/main/ets/models/AuthModel.ets"
  ],
  "candidate_functions": [
    {
      "function_name": "validateLogin",
      "file_path": "src/main/ets/views/Login.ets",
      "start_line": 45,
      "end_line": 78
    }
  ]
}
```

## ⚙️ 配置选项

### 通过环境变量配置

```bash
export FUNCTION_LOCATOR_OLLAMA_HOST="http://localhost:11500"
export FUNCTION_LOCATOR_OLLAMA_LLM_MODEL="qwen3-coder:30b"
export FUNCTION_LOCATOR_LLM_CONTEXT_SIZE=16384
export FUNCTION_LOCATOR_TOP_K_FILES=10
export FUNCTION_LOCATOR_MAX_TOTAL_RESULTS=25
```

### 通过 YAML 配置文件

在 `configs/model_settings.yaml` 中配置：

```yaml
llm:
  model: "qwen3-coder:30b"
  context_size: 16384
  temperature: 0.1
  max_tokens: 2048
```

### 在代码中配置

```python
from function_locator import Config

# 修改配置
Config.OLLAMA_HOST = "http://localhost:11500"
Config.LLM_CONTEXT_SIZE = 16384
Config.TOP_K_FILES = 10
Config.MAX_TOTAL_RESULTS = 25
Config.CACHE_EMBEDDINGS = True
Config.CACHE_SUMMARIES = True
```

## 🔍 调试技巧

### 1. 启用详细日志

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### 2. 启用审计日志

```python
from function_locator import Config
Config.ENABLE_AUDIT_LOGS = True
```

审计日志会保存在 `test_output/llm_audit_logs/` 目录下。

### 3. 查看缓存

```python
from function_locator import Config

# 嵌入向量缓存
print(f"嵌入向量缓存: {Config.get_embeddings_cache_dir()}")

# 摘要缓存
print(f"摘要缓存: {Config.get_summaries_cache_dir()}")
```

### 4. 检查中间结果

定位器会在以下目录保存中间结果：
- `test_output/embeddings_cache/` - 嵌入向量缓存
- `test_output/summaries/` - 文件摘要缓存
- `test_output/llm_audit_logs/` - LLM 审计日志（如果启用）

## ⚠️ 常见问题

### 问题 1: Ollama 连接失败

**错误信息**: `Ollama API request failed`

**解决方案**:
```bash
# 检查 Ollama 服务
curl http://localhost:11500/api/tags

# 如果失败，启动 Ollama
ollama serve
```

### 问题 2: 模型未找到

**错误信息**: `model not found`

**解决方案**:
```bash
# 下载模型
ollama pull qwen3-embedding:8b
ollama pull qwen3-coder:30b
```

### 问题 3: 内存不足

**错误信息**: `out of memory` 或响应很慢

**解决方案**:
- 减小上下文窗口大小: `Config.LLM_CONTEXT_SIZE = 8192`
- 减小 top-k 文件数: `Config.TOP_K_FILES = 3`
- 使用更小的模型

### 问题 4: 找不到相关函数

**可能原因**:
- 问题描述不够具体
- 代码库中没有相关代码
- top-k 值太小

**解决方案**:
- 增加 top-k 值: `--top_k 10`
- 更详细的问题描述
- 检查代码库路径是否正确

## 📈 性能优化

### 1. 启用缓存

```python
Config.CACHE_EMBEDDINGS = True
Config.CACHE_SUMMARIES = True
```

### 2. 调整搜索参数

```python
# 减少搜索文件数（更快但可能遗漏）
Config.TOP_K_FILES = 3

# 增加搜索文件数（更准确但更慢）
Config.TOP_K_FILES = 10
```

### 3. 使用更快的索引

在 `similarity_search.py` 中，可以切换索引类型：
- `faiss` - 最快，需要安装 faiss-cpu
- `annoy` - 较快，需要安装 annoy
- `numpy` - 最慢，但无需额外依赖

## 📝 完整测试流程示例

```bash
# 1. 确保 Ollama 运行
ollama serve

# 2. 检查模型
ollama list

# 3. 运行测试
python scripts/run_function_locator.py \
    --repo_path /home/xiebang/HUAWEI\ 200W/repos/Homogram \
    --problem "修复消息气泡组件的显示问题" \
    --output test_result.json

# 4. 查看结果
cat test_output/test_result.json | jq .

# 5. 检查缓存
ls -lh test_output/embeddings_cache/
ls -lh test_output/summaries/
```

## 🎯 验证测试结果

运行后，检查以下内容：

1. **输出文件存在**: `test_output/locator_output.json`
2. **目标函数不为空**: `result.target_function` 有值
3. **匹配文件列表**: `result.matched_files` 包含相关文件
4. **候选函数列表**: `result.candidate_functions` 包含候选函数
5. **日志输出**: 检查是否有错误信息

## 📚 更多信息

- 工作流程说明: `WORKFLOW_DIAGRAM.md`
- 配置说明: `config.py` 中的文档字符串
- API 文档: `__init__.py` 中的函数文档

