"""
相似度搜索模块 - 基于向量嵌入的 top-k 相似文件搜索。

本模块提供了高效的向量相似度搜索功能，支持多种索引后端（FAISS、Annoy、numpy），
并提供了增量索引、metadata 过滤、索引持久化等高级功能。

主要功能：
    - 向量索引管理：支持 FAISS、Annoy、in-memory numpy 三种索引方式
    - 增量索引：支持追加和更新向量，无需重建整个索引
    - Metadata 过滤：支持基于文件路径、组件等条件的过滤搜索
    - 索引持久化：支持将索引保存到磁盘并从磁盘加载
    - 向后兼容：保持与原有 API 的兼容性

索引类型说明：
    - FAISS: Facebook AI Similarity Search，高性能向量索引库
        * 优点：速度快、支持 GPU、支持增量添加
        * 缺点：需要安装 faiss-cpu 或 faiss-gpu
    - Annoy: Approximate Nearest Neighbors Oh Yeah，Spotify 开发的近似最近邻库
        * 优点：内存占用小、支持持久化
        * 缺点：不支持增量添加（需要重建）
    - numpy: 基于 numpy 的内存计算
        * 优点：无需额外依赖、简单可靠
        * 缺点：速度较慢，适合小规模数据

使用示例：
    ```python
    from function_locator.similarity_search import SimilaritySearch
    from function_locator.types.file_summary import FileSummary
    
    # 创建搜索器
    searcher = SimilaritySearch(top_k=5, index_type="auto")
    
    # 方式1：向后兼容模式（直接传入 file_summaries）
    results = searcher.search(query_embedding, file_summaries)
    
    # 方式2：使用索引模式（推荐，适合大规模数据）
    searcher.rebuild_index(file_summaries)
    results = searcher.search(query_embedding)
    
    # 使用 metadata 过滤
    def filter_by_component(meta):
        return "Component" in meta.get("components", [])
    
    results = searcher.search(
        query_embedding,
        metadata_filter=filter_by_component
    )
    
    # 增量索引
    searcher.append_to_index(new_summaries)
    searcher.update_index(updated_summaries)
    
    # 索引持久化
    searcher.save_index(Path("data/index.faiss"))
    searcher.load_index(Path("data/index.faiss"))
    ```

注意事项：
    - 所有向量在索引前会自动归一化（L2 归一化）
    - 相似度计算使用余弦相似度（cosine similarity）
    - 如果 FAISS/Annoy 不可用，会自动降级到 numpy 模式
    - 索引类型一旦确定，后续操作必须使用相同类型
"""

from typing import List, Tuple, Optional, Dict, Any, Callable
from pathlib import Path
import logging
import numpy as np
import pickle
import sys
from pathlib import Path as PathLib
import json

# 添加项目根目录到路径
project_root = PathLib(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from pangu_model import PanguModel

from .types.file_summary import FileSummary
from .config import Config

logger = logging.getLogger(__name__)

# 尝试导入 FAISS，如果不可用则使用 in-memory numpy
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("FAISS not available, using in-memory numpy for similarity search")

# 尝试导入 Annoy，如果不可用则忽略
try:
    from annoy import AnnoyIndex
    ANNOY_AVAILABLE = True
except ImportError:
    ANNOY_AVAILABLE = False


class SimilaritySearch:
    """
    相似度搜索类 - 基于向量嵌入的 top-k 相似文件搜索器。
    
    本类提供了高效的向量相似度搜索功能，支持多种索引后端，并提供了增量索引、
    metadata 过滤、索引持久化等高级功能。
    
    特性：
        - 支持 FAISS、Annoy、numpy 三种索引后端
        - 自动选择最优索引类型（如果指定 "auto"）
        - 支持增量索引（追加、更新向量）
        - 支持基于 metadata 的过滤搜索
        - 支持索引持久化（保存/加载）
        - 向后兼容原有 API
    
    属性：
        top_k (int): 返回的 top-k 结果数量
        index_type (str): 当前使用的索引类型（"faiss"、"annoy"、"numpy"）
        index_path (Optional[Path]): 索引保存/加载路径
        index: 索引对象（FAISS index 或 AnnoyIndex）
        metadata (List[Dict]): 每个向量的 metadata 列表
        id_to_summary (Dict[int, FileSummary]): ID 到 FileSummary 的映射
        vector_dim (Optional[int]): 向量维度
        use_index (bool): 是否使用索引（False 时使用 in-memory 计算）
    
    使用示例：
        ```python
        # 创建搜索器
        searcher = SimilaritySearch(top_k=5)
        
        # 方式1：向后兼容模式
        results = searcher.search(query_embedding, file_summaries)
        
        # 方式2：索引模式
        searcher.rebuild_index(file_summaries)
        results = searcher.search(query_embedding)
        
        # 使用 metadata 过滤
        def filter_func(meta):
            return "component" in meta.get("file_path", "")
        
        results = searcher.search(
            query_embedding,
            metadata_filter=filter_func
        )
        ```
    """
    
    def __init__(self, 
                 top_k: int = None,
                 index_type: str = "auto",
                 index_path: Optional[Path] = None,
                 pangu_model_path: str = None):
        """
        初始化相似度搜索器（使用 LLM 选择最相似文件）。
        
        Args:
            top_k (int, optional): 返回的 top-k 结果数量。
                如果为 None，则使用 Config.TOP_K_FILES 的默认值。
                默认值：None（使用 Config.TOP_K_FILES）
            
            index_type (str): 已废弃，保留用于兼容性
            
            index_path (Optional[Path]): 已废弃，保留用于兼容性
            
            pangu_model_path (str): Pangu 模型路径
        
        注意：
            - 现在使用 LLM 来选择最相似的文件，而不是 embedding 相似度
        """
        self.top_k = top_k or Config.TOP_K_FILES
        
        # 初始化 Pangu 模型
        self.pangu_model_path = pangu_model_path or getattr(Config, 'PANGU_MODEL_PATH', '/opt/pangu/openPangu-Embedded-7B-V1.1')
        try:
            self.pangu_model = PanguModel(model_path=self.pangu_model_path)
        except Exception as e:
            logger.error(f"初始化 Pangu 模型失败: {e}")
            raise
        
        logger.info(f"Initialized SimilaritySearch with LLM-based file selection")
    
    def rebuild_index(self, file_summaries: List[FileSummary]) -> None:
        """
        重建索引（清空现有索引并重新构建）。
        
        此方法会清空所有现有索引数据，然后使用提供的 file_summaries 重新构建索引。
        适用于首次构建索引或需要完全重建索引的场景。
        
        Args:
            file_summaries (List[FileSummary]): 文件摘要列表，每个摘要必须包含 embedding。
                只有 embedding 不为 None 的摘要会被索引。
        
        Returns:
            None
        
        Raises:
            无直接异常，但会在日志中记录警告（如果没有有效的摘要）
        
        示例：
            ```python
            # 首次构建索引
            searcher = SimilaritySearch()
            searcher.rebuild_index(file_summaries)
            
            # 完全重建索引（清空旧数据）
            searcher.rebuild_index(updated_summaries)
            ```
        
        注意：
            - 此方法会清空所有现有索引数据
            - 只有 embedding 不为 None 的摘要会被索引
            - 向量维度由第一个有效摘要的 embedding 确定
            - 所有向量在索引前会自动归一化（L2 归一化）
        """
        logger.info(f"Rebuilding index with {len(file_summaries)} summaries")
        
        # 清空现有索引
        self.metadata = []
        self.id_to_summary = {}
        self.index = None
        self.use_index = False
        
        # 过滤有 embedding 的 summaries
        summaries_with_embeddings = [
            summary for summary in file_summaries 
            if summary.embedding is not None
        ]
        
        if not summaries_with_embeddings:
            logger.warning("No summaries with embeddings to index")
            return
        
        # 确定向量维度
        first_embedding = summaries_with_embeddings[0].embedding
        self.vector_dim = len(first_embedding)
        
        # 构建索引
        if self.index_type == "faiss" and FAISS_AVAILABLE:
            self._build_faiss_index(summaries_with_embeddings)
        elif self.index_type == "annoy" and ANNOY_AVAILABLE:
            self._build_annoy_index(summaries_with_embeddings)
        else:
            # 使用 in-memory numpy（不构建索引，在搜索时计算）
            self._build_numpy_index(summaries_with_embeddings)
        
        logger.info(f"Index rebuilt successfully with {len(self.metadata)} vectors")
    
    def append_to_index(self, file_summaries: List[FileSummary]) -> None:
        """
        向现有索引追加新的向量（增量索引）。
        
        此方法会将新的向量追加到现有索引中，不会清空现有数据。
        适用于增量添加新文件的场景。
        
        Args:
            file_summaries (List[FileSummary]): 新的文件摘要列表，每个摘要必须包含 embedding。
                只有 embedding 不为 None 的摘要会被追加。
        
        Returns:
            None
        
        Raises:
            ValueError: 如果向量维度与现有索引不匹配
        
        示例：
            ```python
            # 首次构建索引
            searcher.rebuild_index(initial_summaries)
            
            # 追加新文件
            searcher.append_to_index(new_summaries)
            ```
        
        注意：
            - 如果索引为空，会自动调用 rebuild_index()
            - 新向量的维度必须与现有索引的维度一致
            - 对于 Annoy 索引，由于不支持增量添加，会自动重建索引
            - 所有向量在追加前会自动归一化（L2 归一化）
        """
        if not file_summaries:
            return
        
        # 过滤有 embedding 的 summaries
        summaries_with_embeddings = [
            summary for summary in file_summaries 
            if summary.embedding is not None
        ]
        
        if not summaries_with_embeddings:
            logger.warning("No summaries with embeddings to append")
            return
        
        # 如果索引为空，直接重建
        if not self.use_index or self.index is None:
            self.rebuild_index(file_summaries)
            return
        
        # 确定向量维度
        first_embedding = summaries_with_embeddings[0].embedding
        if self.vector_dim is None:
            self.vector_dim = len(first_embedding)
        elif self.vector_dim != len(first_embedding):
            raise ValueError(f"Embedding dimension mismatch: expected {self.vector_dim}, got {len(first_embedding)}")
        
        # 追加到索引
        if self.index_type == "faiss" and FAISS_AVAILABLE:
            self._append_faiss_index(summaries_with_embeddings)
        elif self.index_type == "annoy" and ANNOY_AVAILABLE:
            # Annoy 不支持增量添加，需要重建
            logger.warning("Annoy doesn't support incremental updates, rebuilding index")
            all_summaries = list(self.id_to_summary.values()) + summaries_with_embeddings
            self.rebuild_index(all_summaries)
        else:
            # numpy 模式：追加到 metadata
            self._append_numpy_index(summaries_with_embeddings)
        
        logger.info(f"Appended {len(summaries_with_embeddings)} vectors to index")
    
    def update_index(self, file_summaries: List[FileSummary]) -> None:
        """
        更新索引中的向量（如果已存在则更新，否则追加）。
        
        此方法会检查每个摘要是否已存在于索引中（通过 file_path 判断）。
        如果已存在，则更新该向量；如果不存在，则追加新向量。
        
        Args:
            file_summaries (List[FileSummary]): 要更新的文件摘要列表，每个摘要必须包含 embedding。
                只有 embedding 不为 None 的摘要会被处理。
        
        Returns:
            None
        
        Raises:
            无直接异常，但会在日志中记录警告（如果索引类型不支持直接更新）
        
        示例：
            ```python
            # 更新已存在的文件或添加新文件
            searcher.update_index(updated_summaries)
            ```
        
        注意：
            - 如果索引为空，会自动调用 rebuild_index()
            - 通过 file_path 判断文件是否已存在
            - 对于 FAISS 和 Annoy，由于不支持直接更新，会重建索引（可能较慢）
            - 对于 numpy 模式，会直接更新 metadata
            - 如果只有新文件（无需更新），会自动调用 append_to_index()
        """
        if not file_summaries:
            return
        
        # 如果索引为空，直接重建
        if not self.use_index or self.index is None:
            self.rebuild_index(file_summaries)
            return
        
        # 分离需要更新和需要追加的 summaries
        to_update = []
        to_append = []
        
        for summary in file_summaries:
            if summary.embedding is None:
                continue
            
            # 查找是否已存在（通过 file_path）
            existing_id = None
            for idx, meta in enumerate(self.metadata):
                if meta.get("file_path") == summary.file_path:
                    existing_id = idx
                    break
            
            if existing_id is not None:
                to_update.append((existing_id, summary))
            else:
                to_append.append(summary)
        
        # 更新现有向量（FAISS 不支持直接更新，需要重建）
        if to_update and self.index_type == "faiss":
            logger.warning("FAISS doesn't support direct updates, rebuilding index")
            all_summaries = list(self.id_to_summary.values())
            # 移除要更新的旧项
            for old_id, _ in to_update:
                if old_id < len(all_summaries):
                    all_summaries = [s for s in all_summaries if s.file_path != self.metadata[old_id].get("file_path")]
            # 添加新项
            all_summaries.extend([s for _, s in to_update])
            all_summaries.extend(to_append)
            self.rebuild_index(all_summaries)
        elif to_update:
            # 对于其他索引类型，也采用重建策略（简单但有效）
            logger.info(f"Updating {len(to_update)} vectors by rebuilding index")
            all_summaries = list(self.id_to_summary.values())
            # 移除要更新的旧项
            for old_id, _ in to_update:
                if old_id < len(all_summaries):
                    all_summaries = [s for s in all_summaries if s.file_path != self.metadata[old_id].get("file_path")]
            # 添加新项
            all_summaries.extend([s for _, s in to_update])
            all_summaries.extend(to_append)
            self.rebuild_index(all_summaries)
        else:
            # 只需要追加
            if to_append:
                self.append_to_index(to_append)
    
    def search(self, 
               problem_statement: str,
               file_summaries: List[FileSummary],
               metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[FileSummary, float]]:
        """
        使用 LLM 搜索 top-k 最相似的文件。
        
        Args:
            problem_statement (str): 问题描述文本
            
            file_summaries (List[FileSummary]): 文件摘要列表
            
            metadata_filter (Optional[Callable[[Dict[str, Any]], bool]]): 可选的 metadata 过滤函数。
                函数接收一个 metadata 字典，返回 True 表示保留该结果，False 表示过滤掉。
                默认值：None（不过滤）
        
        Returns:
            List[Tuple[FileSummary, float]]: 相似文件列表，按相似度降序排列。
                每个元组包含：
                - FileSummary: 文件摘要对象
                - float: 相似度分数（0-1，1 表示最相似）
                列表长度最多为 top_k。
        
        Raises:
            ValueError: 如果 problem_statement 为空或 file_summaries 为空
        """
        if not problem_statement:
            raise ValueError("Problem statement cannot be empty")
        
        if not file_summaries:
            raise ValueError("File summaries cannot be empty")
        
        # 应用 metadata 过滤
        if metadata_filter:
            filtered_summaries = []
            for summary in file_summaries:
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                }
                if metadata_filter(meta):
                    filtered_summaries.append(summary)
            file_summaries = filtered_summaries
        
        if not file_summaries:
            logger.warning("No file summaries after filtering")
            return []
        
        # 使用 LLM 选择最相似的文件
        return self._search_with_llm(problem_statement, file_summaries)
    
    def search_with_metadata(self,
                            query_embedding: List[float],
                            metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[int, float, Dict[str, Any]]]:
        """
        搜索并返回 (id, score, metadata) 格式的结果。
        
        此方法与 search() 类似，但返回格式不同，包含更多信息（id 和 metadata）。
        适用于需要直接访问 metadata 或需要 id 的场景。
        
        Args:
            query_embedding (List[float]): 查询向量的 embedding。
                通常是问题描述或查询文本的 embedding 向量。
                向量维度必须与索引中的向量维度一致。
            
            metadata_filter (Optional[Callable[[Dict[str, Any]], bool]]): 可选的 metadata 过滤函数。
                函数接收一个 metadata 字典，返回 True 表示保留该结果，False 表示过滤掉。
                metadata 字典包含以下字段：
                    - "file_name": 文件名
                    - "file_path": 文件路径
                    - "components": 组件列表
                    - "exports": 导出符号列表
                默认值：None（不过滤）
        
        Returns:
            List[Tuple[int, float, Dict[str, Any]]]: 相似文件列表，按相似度降序排列。
                每个元组包含：
                - int: 向量在索引中的 ID
                - float: 相似度分数（0-1，1 表示最相似）
                - Dict[str, Any]: metadata 字典，包含文件相关信息
                列表长度最多为 top_k。
        
        Raises:
            ValueError: 如果 query_embedding 为空
        
        示例：
            ```python
            # 搜索并获取 metadata
            results = searcher.search_with_metadata(query_embedding)
            
            for idx, score, meta in results:
                print(f"ID: {idx}, Score: {score:.3f}")
                print(f"File: {meta['file_name']}")
                print(f"Path: {meta['file_path']}")
                print(f"Components: {meta['components']}")
            
            # 使用 metadata 过滤
            def filter_func(meta):
                return len(meta.get("components", [])) > 0
            
            results = searcher.search_with_metadata(
                query_embedding,
                metadata_filter=filter_func
            )
            ```
        
        注意：
            - 此方法只能用于索引模式（索引必须已构建）
            - 如果索引未构建，会返回空列表并记录警告
            - 所有向量在搜索前会自动归一化（L2 归一化）
            - 相似度计算使用余弦相似度（cosine similarity）
        """
        if not query_embedding:
            raise ValueError("Query embedding cannot be empty")
        
        if not self.use_index or self.index is None:
            logger.warning("Index not built, returning empty results. Call rebuild_index() first.")
            return []
        
        # 执行索引搜索
        if self.index_type == "faiss" and FAISS_AVAILABLE:
            results = self._search_faiss_with_metadata(query_embedding, metadata_filter)
        elif self.index_type == "annoy" and ANNOY_AVAILABLE:
            results = self._search_annoy_with_metadata(query_embedding, metadata_filter)
        else:
            results = self._search_numpy_with_metadata(query_embedding, metadata_filter)
        
        logger.info(f"Found {len(results)} similar files (top-{self.top_k})")
        return results
    
    def save_index(self, index_path: Optional[Path] = None) -> None:
        """
        保存索引到磁盘（支持索引持久化）。
        
        此方法会将索引和 metadata 保存到磁盘，以便后续可以加载使用。
        对于 FAISS 和 Annoy，会保存索引文件；对于 numpy 模式，只保存 metadata。
        
        Args:
            index_path (Optional[Path]): 索引保存路径。
                如果为 None，则使用 self.index_path（在 __init__ 中指定）。
                如果两者都为 None，则不会保存并记录警告。
                默认值：None
        
        Returns:
            None
        
        Raises:
            无直接异常，但会在日志中记录警告（如果路径未指定或保存失败）
        
        示例：
            ```python
            # 构建索引
            searcher.rebuild_index(file_summaries)
            
            # 保存索引
            searcher.save_index(Path("data/index.faiss"))
            
            # 或者在初始化时指定路径
            searcher = SimilaritySearch(index_path=Path("data/index.faiss"))
            searcher.rebuild_index(file_summaries)
            searcher.save_index()  # 使用初始化时指定的路径
            ```
        
        注意：
            - 保存的文件包括索引文件和 metadata 文件（.metadata.pkl）
            - 如果目录不存在，会自动创建
            - 对于 FAISS，保存为 .faiss 文件
            - 对于 Annoy，保存为 .ann 文件
            - 对于 numpy 模式，保存为 pickle 文件
            - metadata 文件包含 metadata、id_to_summary、vector_dim 等信息
        """
        save_path = index_path or self.index_path
        if not save_path:
            logger.warning("No index path specified, cannot save index")
            return
        
        save_path = Path(save_path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.index_type == "faiss" and FAISS_AVAILABLE and self.index is not None:
            # 保存 FAISS 索引
            faiss.write_index(self.index, str(save_path))
            # 保存 metadata
            metadata_path = save_path.with_suffix('.metadata.pkl')
            with open(metadata_path, 'wb') as f:
                pickle.dump({
                    'metadata': self.metadata,
                    'id_to_summary': self.id_to_summary,
                    'vector_dim': self.vector_dim
                }, f)
            logger.info(f"Saved FAISS index to {save_path}")
        elif self.index_type == "annoy" and ANNOY_AVAILABLE and self.index is not None:
            # 保存 Annoy 索引
            self.index.save(str(save_path))
            # 保存 metadata
            metadata_path = save_path.with_suffix('.metadata.pkl')
            with open(metadata_path, 'wb') as f:
                pickle.dump({
                    'metadata': self.metadata,
                    'id_to_summary': self.id_to_summary,
                    'vector_dim': self.vector_dim
                }, f)
            logger.info(f"Saved Annoy index to {save_path}")
        else:
            # 保存 numpy 模式的 metadata
            with open(save_path, 'wb') as f:
                pickle.dump({
                    'metadata': self.metadata,
                    'id_to_summary': self.id_to_summary,
                    'vector_dim': self.vector_dim,
                    'index_type': 'numpy'
                }, f)
            logger.info(f"Saved numpy index metadata to {save_path}")
    
    def load_index(self, index_path: Optional[Path] = None) -> bool:
        """
        从磁盘加载索引。
        
        Args:
            index_path: Path to load index from (uses self.index_path if not provided)
            
        Returns:
            True if index loaded successfully, False otherwise
        """
        load_path = index_path or self.index_path
        if not load_path:
            logger.warning("No index path specified, cannot load index")
            return False
        
        load_path = Path(load_path)
        if not load_path.exists():
            logger.warning(f"Index file not found: {load_path}")
            return False
        
        try:
            metadata_path = load_path.with_suffix('.metadata.pkl')
            if not metadata_path.exists():
                logger.warning(f"Metadata file not found: {metadata_path}")
                return False
            
            # 加载 metadata
            with open(metadata_path, 'rb') as f:
                data = pickle.load(f)
                self.metadata = data['metadata']
                self.id_to_summary = data['id_to_summary']
                self.vector_dim = data['vector_dim']
            
            # 加载索引
            if self.index_type == "faiss" and FAISS_AVAILABLE:
                self.index = faiss.read_index(str(load_path))
                self.use_index = True
                logger.info(f"Loaded FAISS index from {load_path}")
            elif self.index_type == "annoy" and ANNOY_AVAILABLE:
                if self.vector_dim is None:
                    logger.error("Cannot load Annoy index: vector_dim not found in metadata")
                    return False
                self.index = AnnoyIndex(self.vector_dim, 'angular')
                self.index.load(str(load_path))
                self.use_index = True
                logger.info(f"Loaded Annoy index from {load_path}")
            else:
                # numpy 模式：只加载 metadata
                self.use_index = True
                logger.info(f"Loaded numpy index metadata from {load_path}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error loading index from {load_path}: {e}")
            return False
    
    # ==================== 内部方法：索引构建 ====================
    
    def _build_faiss_index(self, summaries: List[FileSummary]) -> None:
        """构建 FAISS 索引。"""
        vectors = []
        for summary in summaries:
            if summary.embedding:
                vectors.append(summary.embedding)
                # 构建 metadata
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                }
                self.metadata.append(meta)
                self.id_to_summary[len(self.metadata) - 1] = summary
        
        if not vectors:
            return
        
        vectors = np.array(vectors, dtype=np.float32)
        
        # 创建 FAISS 索引（使用内积索引，适合归一化后的向量）
        # 对于余弦相似度，向量应该先归一化
        faiss.normalize_L2(vectors)
        self.index = faiss.IndexFlatIP(self.vector_dim)  # Inner Product
        self.index.add(vectors)
        self.use_index = True
        
        logger.info(f"Built FAISS index with {len(vectors)} vectors")
    
    def _build_annoy_index(self, summaries: List[FileSummary]) -> None:
        """构建 Annoy 索引。"""
        self.index = AnnoyIndex(self.vector_dim, 'angular')  # angular distance = cosine distance
        
        for i, summary in enumerate(summaries):
            if summary.embedding:
                self.index.add_item(i, summary.embedding)
                # 构建 metadata
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                }
                self.metadata.append(meta)
                self.id_to_summary[i] = summary
        
        # 构建索引（使用 10 棵树，平衡速度和精度）
        self.index.build(10)
        self.use_index = True
        
        logger.info(f"Built Annoy index with {len(summaries)} vectors")
    
    def _build_numpy_index(self, summaries: List[FileSummary]) -> None:
        """构建 numpy 索引（实际上只是存储 metadata，搜索时计算）。"""
        for summary in summaries:
            if summary.embedding:
                # 构建 metadata
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                    "embedding": summary.embedding,  # 存储 embedding 以便搜索
                }
                self.metadata.append(meta)
                self.id_to_summary[len(self.metadata) - 1] = summary
        
        self.use_index = True
        logger.info(f"Built numpy index (in-memory) with {len(summaries)} vectors")
    
    def _append_faiss_index(self, summaries: List[FileSummary]) -> None:
        """向 FAISS 索引追加向量。"""
        vectors = []
        for summary in summaries:
            if summary.embedding:
                vectors.append(summary.embedding)
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                }
                self.metadata.append(meta)
                self.id_to_summary[len(self.metadata) - 1] = summary
        
        if vectors:
            vectors = np.array(vectors, dtype=np.float32)
            faiss.normalize_L2(vectors)
            self.index.add(vectors)
    
    def _append_numpy_index(self, summaries: List[FileSummary]) -> None:
        """向 numpy 索引追加向量。"""
        for summary in summaries:
            if summary.embedding:
                meta = {
                    "file_name": summary.file_name,
                    "file_path": summary.file_path,
                    "components": summary.components,
                    "exports": summary.exports,
                    "embedding": summary.embedding,
                }
                self.metadata.append(meta)
                self.id_to_summary[len(self.metadata) - 1] = summary
    
    # ==================== 内部方法：搜索 ====================
    
    def _search_in_memory(self, 
                         query_embedding: List[float],
                         file_summaries: List[FileSummary]) -> List[Tuple[FileSummary, float]]:
        """向后兼容的 in-memory 搜索。"""
        if not file_summaries:
            logger.warning("No file summaries provided")
            return []
        
        summaries_with_embeddings = [
            summary for summary in file_summaries 
            if summary.embedding is not None
        ]
        
        if not summaries_with_embeddings:
            logger.warning("No file summaries with embeddings found")
            return []
        
        similarities = []
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)  # 归一化
        
        for summary in summaries_with_embeddings:
            if summary.embedding:
                summary_vec = np.array(summary.embedding, dtype=np.float32)
                summary_vec = summary_vec / np.linalg.norm(summary_vec)  # 归一化
                similarity = float(np.dot(query_vec, summary_vec))
                similarities.append((summary, similarity))
        
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        # 去重：确保返回的文件都是不同的文件路径
        seen_paths = set()
        unique_results = []
        for summary, score in similarities:
            file_path = str(summary.file_path)
            if file_path not in seen_paths:
                seen_paths.add(file_path)
                unique_results.append((summary, score))
                if len(unique_results) >= self.top_k:
                    break
        
        # 如果去重后结果不足，返回所有唯一结果
        if len(unique_results) < self.top_k:
            logger.warning(f"Only found {len(unique_results)} unique files, less than requested {self.top_k}")
        
        return unique_results
    
    def _search_faiss(self, 
                     query_embedding: List[float],
                     metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[FileSummary, float]]:
        """使用 FAISS 索引搜索。"""
        query_vec = np.array([query_embedding], dtype=np.float32)
        faiss.normalize_L2(query_vec)
        
        # 搜索更多结果以确保有足够的唯一文件
        k = self.top_k * 3 if metadata_filter else self.top_k * 2
        scores, indices = self.index.search(query_vec, min(k, self.index.ntotal))
        
        # 去重：确保返回的文件都是不同的文件路径
        seen_paths = set()
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:  # FAISS 返回 -1 表示无效结果
                continue
            
            if idx >= len(self.metadata):
                continue
            
            meta = self.metadata[idx]
            
            # 应用 metadata 过滤
            if metadata_filter and not metadata_filter(meta):
                continue
            
            summary = self.id_to_summary.get(idx)
            if summary:
                file_path = str(summary.file_path)
                if file_path not in seen_paths:
                    seen_paths.add(file_path)
                    results.append((summary, float(score)))
                    
                    if len(results) >= self.top_k:
                        break
        
        if len(results) < self.top_k:
            logger.warning(f"Only found {len(results)} unique files, less than requested {self.top_k}")
        
        return results
    
    def _search_annoy(self,
                     query_embedding: List[float],
                     metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[FileSummary, float]]:
        """使用 Annoy 索引搜索。"""
        # 搜索更多结果以确保有足够的唯一文件
        k = self.top_k * 3 if metadata_filter else self.top_k * 2
        indices, distances = self.index.get_nns_by_vector(
            query_embedding, 
            min(k, len(self.metadata)),
            include_distances=True
        )
        
        # 去重：确保返回的文件都是不同的文件路径
        seen_paths = set()
        results = []
        for idx, distance in zip(indices, distances):
            if idx >= len(self.metadata):
                continue
            
            meta = self.metadata[idx]
            
            # 应用 metadata 过滤
            if metadata_filter and not metadata_filter(meta):
                continue
            
            # 将距离转换为相似度（angular distance -> cosine similarity）
            similarity = 1 - (distance / 2.0)
            
            summary = self.id_to_summary.get(idx)
            if summary:
                file_path = str(summary.file_path)
                if file_path not in seen_paths:
                    seen_paths.add(file_path)
                    results.append((summary, similarity))
                    
                    if len(results) >= self.top_k:
                        break
        
        if len(results) < self.top_k:
            logger.warning(f"Only found {len(results)} unique files, less than requested {self.top_k}")
        
        return results
    
    def _search_numpy(self,
                     query_embedding: List[float],
                     metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[FileSummary, float]]:
        """使用 numpy 计算搜索（in-memory）。"""
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)  # 归一化
        
        similarities = []
        for idx, meta in enumerate(self.metadata):
            # 应用 metadata 过滤
            if metadata_filter and not metadata_filter(meta):
                continue
            
            embedding = meta.get("embedding")
            if not embedding:
                continue
            
            summary_vec = np.array(embedding, dtype=np.float32)
            summary_vec = summary_vec / np.linalg.norm(summary_vec)  # 归一化
            similarity = float(np.dot(query_vec, summary_vec))
            
            summary = self.id_to_summary.get(idx)
            if summary:
                similarities.append((summary, similarity))
        
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        # 去重：确保返回的文件都是不同的文件路径
        seen_paths = set()
        unique_results = []
        for summary, score in similarities:
            file_path = str(summary.file_path)
            if file_path not in seen_paths:
                seen_paths.add(file_path)
                unique_results.append((summary, score))
                if len(unique_results) >= self.top_k:
                    break
        
        if len(unique_results) < self.top_k:
            logger.warning(f"Only found {len(unique_results)} unique files, less than requested {self.top_k}")
        
        return unique_results
    
    def _search_faiss_with_metadata(self,
                                   query_embedding: List[float],
                                   metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[int, float, Dict[str, Any]]]:
        """使用 FAISS 索引搜索，返回 (id, score, metadata) 格式。"""
        query_vec = np.array([query_embedding], dtype=np.float32)
        faiss.normalize_L2(query_vec)
        
        k = self.top_k * 2 if metadata_filter else self.top_k
        scores, indices = self.index.search(query_vec, min(k, self.index.ntotal))
        
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self.metadata):
                continue
            
            meta = self.metadata[idx]
            if metadata_filter and not metadata_filter(meta):
                continue
            
            results.append((idx, float(score), meta))
            if len(results) >= self.top_k:
                break
        
        return results
    
    def _search_annoy_with_metadata(self,
                                   query_embedding: List[float],
                                   metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[int, float, Dict[str, Any]]]:
        """使用 Annoy 索引搜索，返回 (id, score, metadata) 格式。"""
        k = self.top_k * 2 if metadata_filter else self.top_k
        indices, distances = self.index.get_nns_by_vector(
            query_embedding,
            min(k, len(self.metadata)),
            include_distances=True
        )
        
        results = []
        for idx, distance in zip(indices, distances):
            if idx >= len(self.metadata):
                continue
            
            meta = self.metadata[idx]
            if metadata_filter and not metadata_filter(meta):
                continue
            
            similarity = 1 - (distance / 2.0)
            results.append((idx, similarity, meta))
            if len(results) >= self.top_k:
                break
        
        return results
    
    def _search_numpy_with_metadata(self,
                                   query_embedding: List[float],
                                   metadata_filter: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Tuple[int, float, Dict[str, Any]]]:
        """使用 numpy 计算搜索，返回 (id, score, metadata) 格式。"""
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        
        similarities = []
        for idx, meta in enumerate(self.metadata):
            if metadata_filter and not metadata_filter(meta):
                continue
            
            embedding = meta.get("embedding")
            if not embedding:
                continue
            
            summary_vec = np.array(embedding, dtype=np.float32)
            summary_vec = summary_vec / np.linalg.norm(summary_vec)
            similarity = float(np.dot(query_vec, summary_vec))
            
            similarities.append((idx, similarity, meta))
        
        similarities.sort(key=lambda x: x[1], reverse=True)
        return similarities[:self.top_k]
    
    def _search_with_llm(self, problem_statement: str, file_summaries: List[FileSummary]) -> List[Tuple[FileSummary, float]]:
        """
        使用 LLM 选择最相似的文件。
        
        Args:
            problem_statement: 问题描述
            file_summaries: 文件摘要列表
            
        Returns:
            相似文件列表，按相似度降序排列
        """
        # 构建 prompt
        prompt_parts = [
            "请根据问题描述，从以下文件列表中选择最相关的文件。",
            "",
            "问题描述:",
            problem_statement,
            "",
            "文件列表:"
        ]
        
        for i, summary in enumerate(file_summaries, 1):
            prompt_parts.append(f"\n{i}. 文件: {summary.file_name}")
            prompt_parts.append(f"   路径: {summary.file_path}")
            if summary.summary:
                prompt_parts.append(f"   摘要: {summary.summary[:200]}")
            if summary.components:
                prompt_parts.append(f"   组件: {', '.join(summary.components)}")
        
        prompt_parts.extend([
            "",
            f"请选择最相关的 {self.top_k} 个文件，按相关性从高到低排序。",
            "请以 JSON 格式返回结果，格式如下：",
            '[{"index": <文件编号>, "score": <相关性分数 0.0-1.0>, "reason": "<选择理由>"}]',
            "只返回 JSON 数组，不要包含其他说明。"
        ])
        
        prompt = "\n".join(prompt_parts)
        
        try:
            # 调用 LLM
            response = self.pangu_model.generate(
                prompt=prompt,
                max_new_tokens=512,
                temperature=0.3,
                max_length=4096
            )
            
            # 解析响应
            json_start = response.find('[')
            json_end = response.rfind(']') + 1
            
            if json_start < 0 or json_end <= json_start:
                logger.warning("LLM 响应中未找到 JSON，使用默认排序")
                # Fallback: 返回所有文件，分数为 0.5
                return [(summary, 0.5) for summary in file_summaries[:self.top_k]]
            
            json_str = response[json_start:json_end]
            data_list = json.loads(json_str)
            
            # 构建结果列表
            results = []
            for item in data_list:
                index = item.get("index", 0) - 1  # 转换为 0-based
                if 0 <= index < len(file_summaries):
                    score = float(item.get("score", 0.5))
                    results.append((file_summaries[index], score))
            
            # 按分数排序
            results.sort(key=lambda x: x[1], reverse=True)
            
            # 限制返回数量
            results = results[:self.top_k]
            
            logger.info(f"LLM 选择了 {len(results)} 个最相似的文件")
            return results
            
        except Exception as e:
            logger.error(f"LLM 选择文件失败: {e}")
            # Fallback: 返回前 top_k 个文件，分数为 0.5
            return [(summary, 0.5) for summary in file_summaries[:self.top_k]]
    
    def _cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """
        Calculate cosine similarity between two vectors.
        
        Args:
            vec1: First vector
            vec2: Second vector
            
        Returns:
            Cosine similarity score (0-1)
        """
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        
        if norm1 == 0 or norm2 == 0:
            return 0.0
        
        return float(dot_product / (norm1 * norm2))

