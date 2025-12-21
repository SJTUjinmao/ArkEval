"""
文件扫描器 - 递归扫描仓库，找到所有 ArkTS 文件并返回文件信息。

根据模块解析要求：
- 递归扫描仓库
- 过滤忽略路径（`.git`, `node_modules`, `build`）
- 返回 `.ets` 文件路径与原文
- 输出格式：`List[ { file_name, abs_path, content, file_hash } ]`
- 为每个文件计算 hash（mtime + content hash）用于增量索引/缓存决策
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
import hashlib
import os

from .config import Config

logger = logging.getLogger(__name__)


class FileScanner:
    """
    文件扫描器 - 扫描仓库中的 ArkTS 文件。
    
    功能：
        - 递归扫描仓库，找到所有指定扩展名的文件
        - 过滤忽略路径（`.git`, `node_modules`, `build` 等）
        - 读取文件内容
        - 计算文件 hash（mtime + content hash）用于增量索引/缓存决策
        - 返回包含文件信息的字典列表
    """
    
    def __init__(self, extensions: List[str] = None, exclude_patterns: List[str] = None):
        """
        初始化文件扫描器。
        
        Args:
            extensions: 要扫描的文件扩展名列表。默认为 Config.ARKTS_EXTENSIONS。
            exclude_patterns: 要排除的路径模式列表。默认为常见忽略目录。
        """
        self.extensions = extensions or Config.ARKTS_EXTENSIONS
        self.exclude_patterns = exclude_patterns or [
            "node_modules", ".git", "__pycache__", ".idea", 
            "build", "dist", ".vscode", ".vs", ".gradle"
        ]
    
    def scan(self, repo_path: Path) -> List[Dict[str, Any]]:
        """
        扫描仓库中的 ArkTS 文件，返回文件信息列表。
        
        此方法会：
            1. 递归扫描仓库，找到所有指定扩展名的文件
            2. 自动过滤忽略路径
            3. 读取文件内容
            4. 计算文件 hash（mtime + content hash）
        
        Args:
            repo_path: 仓库根目录路径
            
        Returns:
            List[Dict[str, Any]]: 文件信息列表，每个字典包含：
                - file_name (str): 文件名（不含路径）
                - abs_path (str): 文件的绝对路径
                - content (str): 文件内容
                - file_hash (str): 文件 hash（mtime + content hash）
        
        Raises:
            ValueError: 如果仓库路径不存在或不是目录
        
        示例：
            ```python
            scanner = FileScanner()
            files = scanner.scan(Path("./my_repo"))
            for file_info in files:
                print(f"File: {file_info['file_name']}")
                print(f"Hash: {file_info['file_hash']}")
            ```
        """
        if not repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        
        if not repo_path.is_dir():
            raise ValueError(f"Repository path is not a directory: {repo_path}")
        
        # 转换为绝对路径
        repo_path = repo_path.resolve()
        
        logger.info(f"Scanning repository: {repo_path}")
        
        # 找到所有匹配的文件
        all_files = []
        for ext in self.extensions:
            pattern = f"**/*{ext}"
            files = list(repo_path.rglob(pattern))
            all_files.extend(files)
        
        # 去重并排序
        all_files = sorted(set(all_files))
        
        logger.info(f"Found {len(all_files)} files with extensions {self.extensions}")
        
        # 过滤文件并读取内容
        file_infos = []
        for file_path in all_files:
            # 过滤忽略路径
            if self._should_exclude(file_path):
                continue
            
            try:
                file_info = self._read_file_info(file_path, repo_path)
                if file_info:
                    file_infos.append(file_info)
            except Exception as e:
                logger.warning(f"Error reading file {file_path}: {e}")
                continue
        
        logger.info(f"Successfully scanned {len(file_infos)} ArkTS files")
        
        return file_infos
    
    def _should_exclude(self, file_path: Path) -> bool:
        """
        判断文件是否应该被排除。
        
        Args:
            file_path: 文件路径
            
        Returns:
            True 如果文件应该被排除，False 否则
        """
        path_str = str(file_path)
        return any(pattern in path_str for pattern in self.exclude_patterns)
    
    def _read_file_info(self, file_path: Path, repo_path: Path) -> Optional[Dict[str, Any]]:
        """
        读取文件信息（内容、hash 等）。
        
        Args:
            file_path: 文件路径
            repo_path: 仓库根目录路径（用于计算相对路径）
            
        Returns:
            包含文件信息的字典，如果读取失败则返回 None
        """
        if not file_path.is_file():
            return None
        
        try:
            # 读取文件内容
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # 计算文件 hash（mtime + content hash）
            file_hash = self._calculate_file_hash(file_path, content)
            
            # 获取绝对路径
            abs_path = str(file_path.resolve())
            
            # 获取文件名
            file_name = file_path.name
            
            return {
                'file_name': file_name,
                'abs_path': abs_path,
                'content': content,
                'file_hash': file_hash
            }
            
        except Exception as e:
            logger.warning(f"Error reading file {file_path}: {e}")
            return None
    
    def _calculate_file_hash(self, file_path: Path, content: str) -> str:
        """
        计算文件 hash（mtime + content hash）。
        
        使用文件的修改时间（mtime）和内容 hash 来计算唯一标识符，
        用于增量索引/缓存决策。
        
        Args:
            file_path: 文件路径
            content: 文件内容
            
        Returns:
            文件的 hash 字符串（十六进制）
        """
        # 获取文件修改时间
        try:
            mtime = os.path.getmtime(file_path)
            mtime_str = str(int(mtime))
        except OSError:
            mtime_str = "0"
        
        # 计算内容 hash
        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        
        # 组合 mtime 和 content hash
        combined = f"{mtime_str}:{content_hash}"
        file_hash = hashlib.sha256(combined.encode('utf-8')).hexdigest()
        
        return file_hash
    
    def filter_files(self, file_infos: List[Dict[str, Any]], exclude_patterns: List[str] = None) -> List[Dict[str, Any]]:
        """
        根据排除模式过滤文件信息列表。
        
        此方法用于对已扫描的文件信息进行二次过滤。
        
        Args:
            file_infos: 文件信息列表（来自 scan() 方法）
            exclude_patterns: 要排除的路径模式列表。如果为 None，使用默认排除模式。
            
        Returns:
            过滤后的文件信息列表
        
        示例：
            ```python
            files = scanner.scan(repo_path)
            # 额外过滤特定路径
            filtered = scanner.filter_files(files, exclude_patterns=["test", "spec"])
            ```
        """
        if exclude_patterns is None:
            exclude_patterns = self.exclude_patterns
        
        filtered = []
        for file_info in file_infos:
            abs_path = file_info.get('abs_path', '')
            # 检查是否包含任何排除模式
            if not any(pattern in abs_path for pattern in exclude_patterns):
                filtered.append(file_info)
        
        return filtered

