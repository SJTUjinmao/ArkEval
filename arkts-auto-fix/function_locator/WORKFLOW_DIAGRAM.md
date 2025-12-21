# 函数定位工作流示意图

## 📋 完整工作流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        函数定位工作流 (Function Locator)                  │
└─────────────────────────────────────────────────────────────────────────┘

输入: repo_path + problem_statement
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 1: 文件扫描 (FileScanner)                                          │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:86-87                                                  │
│      file_infos = self.file_scanner.scan(repo_path)                    │
│      file_infos = self.file_scanner.filter_files(file_infos)           │
│                                                                          │
│ 作用:                                                                   │
│   • 递归扫描仓库，找到所有 .ets 文件                                    │
│   • 过滤忽略路径 (.git, node_modules, build 等)                         │
│   • 读取文件内容                                                         │
│   • 计算文件 hash (mtime + content hash) 用于缓存                       │
│                                                                          │
│ 输出: List[Dict]                                                        │
│   { file_name, abs_path, content, file_hash }                          │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 2: 文件摘要生成 (FileSummarizer)                                    │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:95-110                                                 │
│      for file_info in file_infos:                                       │
│          summary = self.file_summarizer.summarize(file_path, content)   │
│                                                                          │
│ 作用:                                                                   │
│   • 为每个 ArkTS 文件生成结构化摘要                                     │
│   • 提取文件元信息：组件、导出符号、导入等                              │
│   • 使用缓存加速（如果启用 CACHE_SUMMARIES）                           │
│                                                                          │
│ 输出: List[FileSummary]                                                │
│   { file_name, file_path, components, exports, imports, ... }          │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 3: 向量嵌入生成 (Embedder)                                          │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:112-125                                                │
│      problem_embedding = self.embedder.embed(problem_statement)        │
│      for summary in file_summaries:                                     │
│          self.embedder.embed_summary(summary)                           │
│                                                                          │
│ 作用:                                                                   │
│   • 将问题描述转换为向量 (embedding)                                    │
│   • 将每个文件摘要转换为向量                                             │
│   • 使用 Ollama embedding 模型 (默认: qwen3-embedding:8b)             │
│   • 缓存嵌入向量以加速 (如果启用 CACHE_EMBEDDINGS)                      │
│                                                                          │
│ 输出:                                                                   │
│   • problem_embedding: List[float] (1024维向量)                        │
│   • file_summaries[].embedding: List[float] (每个文件一个向量)         │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 4: 相似度搜索 (SimilaritySearch)                                    │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:127-140                                                │
│      top_files = self.similarity_search.search(problem_embedding,       │
│                                                 file_summaries)         │
│                                                                          │
│ 作用:                                                                   │
│   • 计算问题描述与所有文件摘要的余弦相似度                               │
│   • 按相似度排序，返回 top-k 个最相似的文件                             │
│   • 去重：确保返回至少 TOP_K_FILES (默认5) 个不同的文件                 │
│   • 支持多种索引类型：FAISS / Annoy / numpy (in-memory)                 │
│                                                                          │
│ 算法: 余弦相似度 (Cosine Similarity)                                   │
│   similarity = dot(query_vec, file_vec) / (||query_vec|| * ||file_vec||)│
│                                                                          │
│ 输出: List[Tuple[FileSummary, float]]                                  │
│   [(file_summary_1, score_1), ..., (file_summary_k, score_k)]          │
│   按相似度降序排列，至少5个不同文件                                     │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 5: 函数提取 (FunctionExtractor)                                    │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:150-156                                                │
│      for file_summary, similarity_score in top_files:                   │
│          functions = self.function_extractor.extract(file_path)        │
│                                                                          │
│ 作用:                                                                   │
│   • 从匹配的文件中提取所有函数/方法                                      │
│   • 使用正则表达式 + 括号计数提取函数                                   │
│   • 支持普通函数和类方法                                                 │
│   • 提取函数信息：名称、参数、返回类型、代码、行号等                     │
│                                                                          │
│ 输出: List[FunctionInfo]                                                │
│   { function_name, parameters, return_type, code, start_line, ... }   │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 6: LLM 过滤与选择 (LLMFilter)                                      │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:161-177                                                │
│      candidate_decisions = self.llm_filter.filter(                     │
│          problem_statement, file_summary, functions)                   │
│      for decision in candidate_decisions:                               │
│          if decision.need_modify:                                       │
│              target_function = decision.function                        │
│                                                                          │
│ 作用:                                                                   │
│   • 使用 LLM 分析每个函数是否需要修改                                    │
│   • 根据问题描述和文件上下文判断函数相关性                               │
│   • 返回候选决策列表，包含：                                             │
│     - need_modify: 是否需要修改                                         │
│     - reason: 判断理由                                                  │
│     - function: 函数信息                                                │
│   • 选择第一个 need_modify=True 的函数作为目标函数                      │
│                                                                          │
│ LLM Prompt 包含:                                                       │
│   • 问题描述 (problem_statement)                                        │
│   • 文件摘要 (file_summary)                                            │
│   • 函数列表 (functions)                                                │
│                                                                          │
│ 输出: List[CandidateDecision]                                           │
│   选择第一个 need_modify=True 的函数                                    │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 步骤 7: 结果构建与输出 (OutputWriter)                                   │
│ ─────────────────────────────────────────────────────────────────────── │
│ 代码: locator.py:192-204                                                │
│      result = LocatorResult(                                           │
│          problem_statement, target_function, reasoning,                 │
│          code_before, candidate_functions, matched_files)             │
│      self.output_writer.write(result, "locator_output.json")          │
│                                                                          │
│ 作用:                                                                   │
│   • 构建定位结果对象 (LocatorResult)                                    │
│   • 包含目标函数、推理过程、候选函数列表等                              │
│   • 写入 JSON 文件到输出目录                                            │
│   • 可选：写入缓存目录用于调试/回放                                     │
│                                                                          │
│ 输出: LocatorResult                                                     │
│   {                                                                     │
│     problem_statement: str,                                             │
│     target_function: FunctionInfo,                                      │
│     reasoning: str,                                                     │
│     code_before: str,                                                   │
│     candidate_functions: List[FunctionInfo],                            │
│     matched_files: List[str]                                            │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
  │
  ▼
输出: LocatorResult (JSON 文件 + 对象)
```

## 🔧 核心模块说明

### 1. FileScanner (文件扫描器)
**文件**: `file_scanner.py`

**主要方法**:
- `scan(repo_path)`: 递归扫描仓库，返回所有 .ets 文件信息
- `filter_files(file_infos)`: 过滤忽略路径

**关键代码**:
```python
# 扫描所有 .ets 文件
for ext in self.extensions:
    pattern = f"**/*{ext}"
    files = list(repo_path.rglob(pattern))

# 计算文件 hash (用于缓存)
file_hash = hashlib.md5(
    f"{mtime}_{content_hash}".encode()
).hexdigest()
```

---

### 2. FileSummarizer (文件摘要生成器)
**文件**: `file_summarizer.py`

**主要方法**:
- `summarize(file_path, content)`: 生成文件结构化摘要

**提取的信息**:
- 组件名称 (components)
- 导出符号 (exports)
- 导入依赖 (imports)
- 文件元数据

**缓存机制**:
- 如果启用 `CACHE_SUMMARIES`，会缓存摘要到 `test_output/summaries/`

---

### 3. Embedder (向量嵌入生成器)
**文件**: `embedder.py`

**主要方法**:
- `embed(text)`: 将文本转换为向量
- `embed_summary(summary)`: 将文件摘要转换为向量

**技术细节**:
- 使用 Ollama API 调用 embedding 模型
- 向量维度: 1024 (由模型决定)
- 支持批量处理和多线程
- 缓存机制: 相同文本的 embedding 会被缓存

**关键代码**:
```python
# 调用 Ollama embedding API
response = requests.post(
    f"{self.ollama_host}/api/embeddings",
    json={"model": self.model_name, "prompt": text}
)
embedding = response.json()["embedding"]
```

---

### 4. SimilaritySearch (相似度搜索器)
**文件**: `similarity_search.py`

**主要方法**:
- `search(query_embedding, file_summaries)`: 搜索最相似的文件

**搜索算法**:
1. **余弦相似度计算**:
   ```python
   similarity = dot(query_vec, file_vec) / (||query_vec|| * ||file_vec||)
   ```

2. **去重逻辑**:
   ```python
   seen_paths = set()
   unique_results = []
   for summary, score in similarities:
       if str(summary.file_path) not in seen_paths:
           seen_paths.add(str(summary.file_path))
           unique_results.append((summary, score))
   ```

**支持的索引类型**:
- **FAISS**: 高性能向量索引 (需要安装 faiss-cpu)
- **Annoy**: 近似最近邻搜索 (需要安装 annoy)
- **numpy**: 内存计算 (默认，无需额外依赖)

**配置**:
- `TOP_K_FILES = 5`: 默认返回5个最相似的文件
- 确保返回至少5个**不同**的文件

---

### 5. FunctionExtractor (函数提取器)
**文件**: `function_extractor.py`

**主要方法**:
- `extract(file_path)`: 从文件中提取所有函数

**提取策略**:
1. **正则表达式匹配**:
   ```python
   # 普通函数模式
   function_pattern = re.compile(
       r'(?:public\s+|private\s+)?'
       r'(?:async\s+)?'
       r'function\s+(\w+)\s*\([^)]*\)\s*\{'
   )
   ```

2. **括号计数**: 确定函数边界

**提取的信息**:
- 函数名称
- 参数列表
- 返回类型
- 函数代码
- 起始/结束行号
- 字节位置

---

### 6. LLMFilter (LLM 过滤器)
**文件**: `llm_filter.py`

**主要方法**:
- `filter(problem_statement, file_summary, functions)`: 使用 LLM 过滤函数

**工作流程**:
1. **构建 Prompt**:
   - 包含问题描述
   - 文件摘要信息
   - 函数列表

2. **调用 LLM**:
   ```python
   response = requests.post(
       f"{self.ollama_host}/api/generate",
       json={
           "model": self.model_name,
           "prompt": prompt,
           "temperature": self.temperature
       }
   )
   ```

3. **解析响应**:
   - 提取每个函数的决策 (need_modify, reason)
   - 返回 `CandidateDecision` 列表

**两种模式**:
- **light_rerank**: 轻量级重排序 (最小 prompt)
- **detailed_locate**: 详细定位 (完整上下文)

**审计日志**:
- 如果启用 `ENABLE_AUDIT_LOGS`，会记录所有 LLM 输入/输出

---

### 7. OutputWriter (输出写入器)
**文件**: `output_writer.py`

**主要方法**:
- `write(result, filename)`: 写入定位结果到 JSON 文件

**输出格式**:
```json
{
  "problem_statement": "...",
  "target_function": {
    "function_name": "...",
    "file_path": "...",
    "start_line": 123,
    "end_line": 145,
    "code": "..."
  },
  "reasoning": "...",
  "code_before": "...",
  "candidate_functions": [...],
  "matched_files": [...]
}
```

**缓存机制**:
- 如果启用 `CACHE_SUMMARIES`，会同时写入缓存目录

---

## 📊 数据流图

```
问题描述 (problem_statement)
    │
    ├─→ Embedder ──→ problem_embedding (向量)
    │
仓库路径 (repo_path)
    │
    ├─→ FileScanner ──→ file_infos (文件列表)
    │
    ├─→ FileSummarizer ──→ file_summaries (文件摘要)
    │                          │
    │                          ├─→ Embedder ──→ embeddings (向量)
    │                          │
    │                          └─→ SimilaritySearch ←─ problem_embedding
    │                                                  │
    │                                                  └─→ top_files (最相似的文件)
    │                                                       │
    │                                                       ├─→ FunctionExtractor ──→ functions
    │                                                       │
    │                                                       └─→ LLMFilter ←─ problem_statement
    │                                                                         │
    │                                                                         └─→ target_function
    │                                                                              │
    └─────────────────────────────────────────────────────────────────────────────┘
                                                                                    │
                                                                                    ▼
                                                                          OutputWriter ──→ JSON 文件
```

## ⚙️ 配置参数

### Config (config.py)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `TOP_K_FILES` | 5 | 相似度搜索返回的文件数量 |
| `CACHE_EMBEDDINGS` | True | 是否缓存嵌入向量 |
| `CACHE_SUMMARIES` | True | 是否缓存文件摘要 |
| `ENABLE_AUDIT_LOGS` | False | 是否启用 LLM 审计日志 |
| `OLLAMA_HOST` | http://localhost:11500 | Ollama API 地址 |
| `OLLAMA_EMBEDDING_MODEL` | qwen3-embedding:8b | 嵌入模型名称 |
| `OLLAMA_LLM_MODEL` | qwen3-coder:30b | LLM 模型名称 |

## 🎯 关键优化点

1. **去重机制**: 确保返回至少5个不同的文件，避免同一文件的多个函数
2. **缓存策略**: 嵌入向量和文件摘要都会被缓存，加速重复分析
3. **多索引支持**: 支持 FAISS/Annoy/numpy 多种索引类型
4. **错误处理**: 每个步骤都有异常处理，确保流程稳定
5. **可配置性**: 所有关键参数都可通过 Config 配置

## 📝 使用示例

```python
from function_locator import FunctionLocator
from pathlib import Path

# 初始化定位器
locator = FunctionLocator(
    ollama_host="http://localhost:11500",
    embedding_model="qwen3-embedding:8b",
    llm_model="qwen3-coder:30b"
)

# 执行定位
result = locator.locate(
    repo_path=Path("./my_repo"),
    problem_statement="修复登录功能的验证逻辑"
)

# 获取结果
print(f"目标函数: {result.target_function.function_name}")
print(f"文件: {result.target_function.file_path}")
print(f"行号: {result.target_function.start_line}-{result.target_function.end_line}")
print(f"匹配文件数: {len(result.matched_files)}")
```

