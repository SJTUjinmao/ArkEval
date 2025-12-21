"""
输出写入器 - 把最终定位结果写入磁盘，并同时写入缓存（用于调试/回放）。

根据模块解析要求：
- JSON schema 校验（pydantic），并写入 human-readable JSON（indent）
- 同时保留原始 LLM 响应日志（供审计）
"""

from pathlib import Path
from typing import Optional
import json
import logging
import time
import hashlib

from .types.locator_result import LocatorResult
from .config import Config

logger = logging.getLogger(__name__)


class OutputWriter:
    """
    输出写入器 - 将定位结果写入磁盘并缓存。
    
    功能：
        - 写入主输出文件（test_output/locator_output.json）
        - 写入缓存目录（用于调试/回放）
        - JSON schema 校验
        - 保留审计日志
    """
    
    def __init__(self, output_dir: Optional[Path] = None, cache_dir: Optional[Path] = None):
        """
        初始化输出写入器。
        
        Args:
            output_dir: 输出目录。默认为 Config.OUTPUT_DIR。
            cache_dir: 缓存目录。默认为 test_output/summaries。
        """
        self.output_dir = output_dir or Config.OUTPUT_DIR or Path("test_output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 只有在启用缓存时才创建 summaries 目录
        if Config.CACHE_SUMMARIES:
            self.cache_dir = cache_dir or (self.output_dir / "summaries")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.cache_dir = None
    
    def write(self, result: LocatorResult, filename: str = "locator_output.json"):
        """
        写入定位结果到 JSON 文件（包含 schema 校验和缓存）。
        
        Args:
            result: LocatorResult 对象
            filename: 输出文件名
        """
        output_path = self.output_dir / filename
        
        try:
            # Pydantic schema 校验（自动进行）
            # 转换为字典
            result_dict = result.dict()
            
            # 写入 human-readable JSON（indent=2）
            json_str = json.dumps(
                result_dict,
                indent=2,
                ensure_ascii=False,
                default=str
            )
            
            output_path.write_text(json_str, encoding='utf-8')
            logger.info(f"Locator result written to {output_path}")
            
            # 同时写入缓存（用于调试/回放，如果启用）
            if self.cache_dir:
                self._write_to_cache(result, result_dict)
            
        except Exception as e:
            logger.error(f"Error writing output to {output_path}: {e}")
            raise
    
    def _write_to_cache(self, result: LocatorResult, result_dict: dict):
        """
        写入缓存（用于调试/回放）。
        
        Args:
            result: LocatorResult 对象
            result_dict: 结果字典
        """
        if not self.cache_dir:
            return
        
        try:
            # 生成缓存文件名（基于问题描述和时间的 hash）
            problem_hash = hashlib.md5(
                result.problem_statement.encode('utf-8')
            ).hexdigest()[:8]
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            cache_filename = f"locator_result_{timestamp}_{problem_hash}.json"
            cache_path = self.cache_dir / cache_filename
            
            # 添加元数据
            cache_data = {
                "metadata": {
                    "timestamp": timestamp,
                    "problem_hash": problem_hash,
                    "candidates_count": len(result.candidates),
                    "version": "0.1.0"
                },
                "result": result_dict
            }
            
            json_str = json.dumps(
                cache_data,
                indent=2,
                ensure_ascii=False,
                default=str
            )
            
            cache_path.write_text(json_str, encoding='utf-8')
            logger.debug(f"Result cached to {cache_path}")
            
        except Exception as e:
            logger.warning(f"Failed to write cache: {e}")
    
    def write_pretty(self, result: LocatorResult, filename: str = "locator_output.json"):
        """
        写入格式化的定位结果（更易读的格式）。
        
        Args:
            result: LocatorResult 对象
            filename: 输出文件名
        """
        output_path = self.output_dir / filename
        
        try:
            # 创建更易读的格式
            output_data = {
                "problem_statement": result.problem_statement,
                "candidates": [
                    {
                        "file": cand.file,
                        "function": cand.function.function_name,
                        "reason": cand.reason,
                        "score": cand.score,
                        "lines": f"{cand.function.start_line}-{cand.function.end_line}"
                    }
                    for cand in result.candidates
                ],
                "target_function": {
                    "name": result.target_function.function_name if result.target_function else None,
                    "file": result.target_function.file_path if result.target_function else None,
                    "start_line": result.target_function.start_line if result.target_function else None,
                    "end_line": result.target_function.end_line if result.target_function else None,
                    "code": result.code_before
                } if result.target_function else None,
                "reasoning": result.reasoning,
                "matched_files": result.matched_files,
                "candidate_functions_count": len(result.candidate_functions) if result.candidate_functions else 0
            }
            
            json_str = json.dumps(
                output_data,
                indent=2,
                ensure_ascii=False,
                default=str
            )
            
            output_path.write_text(json_str, encoding='utf-8')
            
            logger.info(f"Locator result written to {output_path}")
            
            # 同时写入缓存（如果启用）
            if self.cache_dir:
                self._write_to_cache(result, output_data)
            
        except Exception as e:
            logger.error(f"Error writing output to {output_path}: {e}")
            raise


