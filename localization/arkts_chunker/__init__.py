"""
Experimental ArkTS (.ets) chunking utilities.

本包与现有 `localization_engine` 索引/分块逻辑完全解耦，只用于：
- 对指定 ArkTS `.ets` 文件做分块预览与实验；
- 方便手动对比 ArkTS 专用分块 vs 现有纯文本 fallback。

不会写入 Milvus、也不会修改 `.codephoenix` 既有结构。
"""

__all__ = ["__version__"]

__version__ = "0.0.1"
