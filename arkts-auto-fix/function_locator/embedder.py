"""
Embedder for generating embeddings using Ollama qwen3-embedding model.

支持批量、并发、缓存以及向量持久化。
"""

from typing import List, Optional, Union
import logging
import requests
import json
import hashlib
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    logging.warning("NumPy not available, embeddings will be returned as lists")

from .config import Config
from .types.file_summary import FileSummary

logger = logging.getLogger(__name__)


class Embedder:
    """Generates embeddings for text using Ollama embedding model.
    
    支持单个文本、批量文本、文件摘要的嵌入向量生成。
    支持缓存、并发处理和向量持久化。
    """
    
    def __init__(self, 
                 ollama_host: str = None,
                 model_name: str = None,
                 use_cache: bool = True,
                 max_workers: int = 4):
        """
        Initialize embedder.
        
        Args:
            ollama_host: Ollama API host URL
            model_name: Name of the embedding model (从 config.py 读取)
            use_cache: Whether to use cached embeddings
            max_workers: Maximum number of concurrent workers for batch processing
        """
        self.ollama_host = ollama_host or Config.OLLAMA_HOST
        self.model_name = model_name or Config.OLLAMA_EMBEDDING_MODEL
        self.use_cache = use_cache and Config.CACHE_EMBEDDINGS
        self.max_workers = max_workers
        self.cache_dir = Config.get_embeddings_cache_dir()
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def embed(self, text: str, return_numpy: bool = False) -> Union[List[float], "np.ndarray"]:
        """
        Generate embedding for text.
        
        Args:
            text: Text to embed
            return_numpy: If True, return numpy array; otherwise return list
            
        Returns:
            Embedding vector as list of floats or numpy array
        """
        # Check cache first
        if self.use_cache:
            cached = self._load_from_cache(text)
            if cached:
                if return_numpy and NUMPY_AVAILABLE:
                    return np.array(cached)
                return cached
        
        # Generate embedding via Ollama API
        try:
            embedding = self._generate_embedding(text)
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            raise
        
        # Save to cache
        if self.use_cache:
            self._save_to_cache(text, embedding)
        
        # Convert to numpy if requested
        if return_numpy and NUMPY_AVAILABLE:
            return np.array(embedding)
        
        return embedding
    
    def embed_texts(self, texts: List[str], batch_size: int = 32, return_numpy: bool = False) -> List[Union[List[float], "np.ndarray"]]:
        """
        批量生成文本的嵌入向量（支持并发处理）。
        
        Args:
            texts: List of texts to embed
            batch_size: Number of texts to process in each batch
            return_numpy: If True, return numpy arrays; otherwise return lists
            
        Returns:
            List of embedding vectors
        """
        if not texts:
            return []
        
        # 检查缓存，分离需要计算和已缓存的文本
        results = [None] * len(texts)
        texts_to_embed = []
        indices_to_embed = []
        
        for i, text in enumerate(texts):
            if self.use_cache:
                cached = self._load_from_cache(text)
                if cached:
                    if return_numpy and NUMPY_AVAILABLE:
                        results[i] = np.array(cached)
                    else:
                        results[i] = cached
                    continue
            
            texts_to_embed.append(text)
            indices_to_embed.append(i)
        
        # 如果没有需要计算的文本，直接返回
        if not texts_to_embed:
            return results
        
        # 批量并发处理
        logger.info(f"Processing {len(texts_to_embed)} texts in batches of {batch_size}")
        
        # 分批处理
        for batch_start in range(0, len(texts_to_embed), batch_size):
            batch_end = min(batch_start + batch_size, len(texts_to_embed))
            batch_texts = texts_to_embed[batch_start:batch_end]
            batch_indices = indices_to_embed[batch_start:batch_end]
            
            # 并发处理当前批次
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_index = {
                    executor.submit(self._generate_embedding, text): (i, text)
                    for i, text in zip(batch_indices, batch_texts)
                }
                
                for future in as_completed(future_to_index):
                    index, text = future_to_index[future]
                    try:
                        embedding = future.result()
                        
                        # 保存到缓存
                        if self.use_cache:
                            self._save_to_cache(text, embedding)
                        
                        # 转换格式
                        if return_numpy and NUMPY_AVAILABLE:
                            results[index] = np.array(embedding)
                        else:
                            results[index] = embedding
                            
                    except Exception as e:
                        logger.error(f"Error generating embedding for text at index {index}: {e}")
                        # 使用空向量作为占位符
                        results[index] = [0.0] * Config.EMBEDDING_DIMENSION
                        if return_numpy and NUMPY_AVAILABLE:
                            results[index] = np.array(results[index])
        
        return results
    
    def embed_summary(self, file_summary: FileSummary, use_file_hash: bool = True) -> FileSummary:
        """
        Generate embedding for a file summary.
        
        支持使用 file_hash 进行缓存，避免重复计算。
        
        Args:
            file_summary: FileSummary object
            use_file_hash: If True, use file_path hash for caching; otherwise use text hash
            
        Returns:
            FileSummary with embedding added
        """
        # Create text representation for embedding
        text = self._summary_to_text(file_summary)
        
        # 如果使用 file_hash，优先从文件路径的 hash 加载缓存
        if use_file_hash and self.use_cache:
            file_hash = self._get_file_hash(file_summary.file_path)
            cached = self._load_from_cache_by_hash(file_hash)
            if cached:
                file_summary.embedding = cached
                return file_summary
        
        # Generate embedding
        embedding = self.embed(text)
        
        # 如果使用 file_hash，保存到缓存
        if use_file_hash and self.use_cache:
            file_hash = self._get_file_hash(file_summary.file_path)
            self._save_to_cache_by_hash(file_hash, embedding)
        
        # Update file summary
        file_summary.embedding = embedding
        
        return file_summary
    
    def _generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding using Ollama API.
        
        Args:
            text: Text to embed
            
        Returns:
            Embedding vector
        """
        url = f"{self.ollama_host}/api/embeddings"
        payload = {
            "model": self.model_name,
            "prompt": text
        }
        
        # 重试机制：最多重试 3 次，每次超时时间递增
        max_retries = 3
        timeouts = [180, 300, 600]  # 3分钟、5分钟、10分钟
        
        for attempt in range(max_retries):
            try:
                timeout = timeouts[min(attempt, len(timeouts) - 1)]
                if attempt > 0:
                    logger.info(f"Retrying embedding request (attempt {attempt + 1}/{max_retries}) with timeout {timeout}s...")
                
                response = requests.post(url, json=payload, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                embedding = result.get("embedding", [])
                
                if embedding:
                    return embedding
                else:
                    logger.warning(f"Empty embedding returned, retrying...")
                    
            except requests.exceptions.Timeout as e:
                logger.warning(f"Embedding request timeout (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    logger.error(f"All retry attempts failed for embedding request")
                    raise
            except requests.exceptions.RequestException as e:
                logger.error(f"Ollama API request failed: {e}")
                if attempt == max_retries - 1:
                    raise
                # 对于非超时错误，等待一段时间后重试
                time.sleep(2 ** attempt)  # 指数退避：2秒、4秒、8秒
        
        raise RuntimeError("Failed to generate embedding after all retries")
    
    def _summary_to_text(self, file_summary: FileSummary) -> str:
        """
        Convert file summary to text for embedding.
        
        Args:
            file_summary: FileSummary object
            
        Returns:
            Text representation
        """
        parts = [
            f"File: {file_summary.file_name}",
            f"Summary: {file_summary.summary}",
        ]
        
        if file_summary.main_entry:
            parts.append(f"Main entry: {file_summary.main_entry}")
        
        if file_summary.exports:
            parts.append(f"Exports: {', '.join(file_summary.exports)}")
        
        if file_summary.components:
            parts.append(f"Components: {', '.join(file_summary.components)}")
        
        if file_summary.state_management:
            parts.append(f"State management: {', '.join(file_summary.state_management)}")
        
        return "\n".join(parts)
    
    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text."""
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def _get_file_hash(self, file_path: str) -> str:
        """Generate hash for file path (used for file_hash caching)."""
        return hashlib.md5(file_path.encode('utf-8')).hexdigest()
    
    def _load_from_cache(self, text: str) -> Optional[List[float]]:
        """Load embedding from cache if available (using text hash)."""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return data.get("embedding")
            except Exception as e:
                logger.warning(f"Error loading embedding cache: {e}")
        return None
    
    def _load_from_cache_by_hash(self, file_hash: str) -> Optional[List[float]]:
        """Load embedding from cache using file hash."""
        cache_file = self.cache_dir / f"file_{file_hash}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return data.get("embedding")
            except Exception as e:
                logger.warning(f"Error loading embedding cache by hash: {e}")
        return None
    
    def _save_to_cache(self, text: str, embedding: List[float]):
        """Save embedding to cache (using text hash)."""
        cache_key = self._get_cache_key(text)
        cache_file = self.cache_dir / f"{cache_key}.json"
        try:
            data = {"embedding": embedding}
            cache_file.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Error saving embedding cache: {e}")
    
    def _save_to_cache_by_hash(self, file_hash: str, embedding: List[float]):
        """Save embedding to cache using file hash."""
        cache_file = self.cache_dir / f"file_{file_hash}.json"
        try:
            data = {"embedding": embedding}
            cache_file.write_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Error saving embedding cache by hash: {e}")

