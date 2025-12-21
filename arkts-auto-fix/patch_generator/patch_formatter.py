"""
补丁格式化器 - 将 old_code 与 new_code 转换成 unified_diff。

根据模块解析要求：
- 使用 python 库如 difflib.unified_diff 或 git 命令行生成更可靠 diff
- 提供 fallback（若行号不匹配，按代码片段 fuzzy search 定位并替换）
"""

from pathlib import Path
from typing import Optional
import logging
import difflib
import subprocess

logger = logging.getLogger(__name__)


class PatchFormatter:
    """
    补丁格式化器 - 生成 unified diff。
    
    功能：
        - 使用 difflib 生成 diff
        - 支持 git 命令行生成（如果可用）
        - 提供 fallback 机制
    """
    
    def __init__(self, use_git: bool = True):
        """
        初始化补丁格式化器。
        
        Args:
            use_git: 是否优先使用 git 生成 diff
        """
        self.use_git = use_git
    
    def format_diff(self,
                   old_code: str,
                   new_code: str,
                   file_path: str,
                   start_line: int = 1,
                   context_lines: int = 3) -> str:
        """
        格式化补丁为 unified diff。
        
        Args:
            old_code: 原始代码
            new_code: 新代码
            file_path: 文件路径
            context_lines: 上下文行数
            
        Returns:
            unified diff 字符串
        """
        # 尝试使用 git（如果可用且 use_git=True）
        if self.use_git:
            git_diff = self._generate_git_diff(old_code, new_code, file_path, start_line)
            if git_diff:
                return git_diff
        
        # 使用 difflib 作为 fallback
        return self._generate_difflib_diff(old_code, new_code, file_path, start_line, context_lines)
    
    def _generate_git_diff(self,
                          old_code: str,
                          new_code: str,
                          file_path: str,
                          start_line: int = 1) -> Optional[str]:
        """
        使用 git 生成 diff。
        
        Args:
            old_code: 原始代码
            new_code: 新代码
            file_path: 文件路径
            
        Returns:
            unified diff 字符串，如果失败则返回 None
        """
        try:
            # 创建临时文件
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                old_file = Path(tmpdir) / "old.ets"
                new_file = Path(tmpdir) / "new.ets"
                
                old_file.write_text(old_code, encoding='utf-8')
                new_file.write_text(new_code, encoding='utf-8')
                
                # 使用 git diff
                result = subprocess.run(
                    ['git', 'diff', '--no-index', '--unified=3', str(old_file), str(new_file)],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    # 替换文件路径
                    diff = result.stdout.replace(str(old_file), file_path)
                    diff = diff.replace(str(new_file), file_path)
                    return diff
                    
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
            logger.debug(f"Git diff failed, using difflib: {e}")
        
        return None
    
    def _generate_difflib_diff(self,
                              old_code: str,
                              new_code: str,
                              file_path: str,
                              start_line: int = 1,
                              context_lines: int = 3) -> str:
        """
        使用 difflib 生成 unified diff。
        
        Args:
            old_code: 原始代码
            new_code: 新代码
            file_path: 文件路径（可以是相对路径或文件名）
            start_line: 起始行号（用于修正 hunk 行号）
            context_lines: 上下文行数
            
        Returns:
            unified diff 字符串
        """
        old_lines = old_code.splitlines(keepends=True)
        new_lines = new_code.splitlines(keepends=True)
        
        # 使用提供的文件路径（可能是相对路径）
        # 如果路径包含目录，使用完整路径；否则只使用文件名
        if '/' in file_path or '\\' in file_path:
            rel_path = file_path.replace('\\', '/')
        else:
            rel_path = file_path
        
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm='\n',  # 使用换行符，确保每行独立
            n=context_lines
        )
        
        # 转换为标准格式，并修正行号信息
        diff_lines = []
        for line in diff:
            # difflib 会在每行末尾添加换行符，我们需要处理
            line = line.rstrip('\n')
            if line:  # 跳过空行（但保留有内容的行）
                diff_lines.append(line)
        
        if not diff_lines:
            return ''
        
        # 修正 hunk 行号（如果 start_line > 1）
        corrected_lines = []
        for line in diff_lines:
            # 查找 hunk 头部行（格式：@@ -old_start,old_count +new_start,new_count @@）
            if line.startswith('@@'):
                # 解析当前 hunk 行号
                # 格式：@@ -old_start,old_count +new_start,new_count @@
                import re
                match = re.match(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@', line)
                if match:
                    old_start, old_count, new_start, new_count = map(int, match.groups())
                    # 修正为实际的绝对行号
                    if start_line > 1:
                        corrected_old_start = start_line + old_start - 1
                        corrected_new_start = start_line + new_start - 1
                        corrected_lines.append(f'@@ -{corrected_old_start},{old_count} +{corrected_new_start},{new_count} @@')
                    else:
                        corrected_lines.append(line)
                else:
                    corrected_lines.append(line)
            else:
                corrected_lines.append(line)
        
        # 确保每行都有换行符
        return '\n'.join(corrected_lines) + '\n'
    
    def format_fallback_diff(self,
                            old_code: str,
                            new_code: str,
                            file_path: str,
                            start_line: int,
                            end_line: int) -> str:
        """
        Fallback diff 生成（当行号不匹配时使用 fuzzy search）。
        
        Args:
            old_code: 原始代码
            new_code: 新代码
            file_path: 文件路径
            start_line: 起始行号
            end_line: 结束行号
            
        Returns:
            unified diff 字符串
        """
        # 简单的 fallback：直接生成 diff
        return self._generate_difflib_diff(old_code, new_code, file_path, context_lines=3)

