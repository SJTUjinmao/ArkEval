"""
补丁应用器 - 对文件应用 unified_diff 或区块替换。

根据模块解析要求：
- 优先使用 git apply
- 若失败则用 start_line/end_line 区块替换
- 在修改前先 git checkout -- file 保存工作树
- 应用后 git add 并创建临时分支 autofix/<timestamp>
"""

from pathlib import Path
from typing import Optional, Tuple
import logging
import subprocess
import shutil

logger = logging.getLogger(__name__)


class PatchApplier:
    """
    补丁应用器 - 应用补丁到代码库。
    
    功能：
        - 使用 git apply 应用 unified diff
        - Fallback 到区块替换
        - 创建临时分支
        - 保存工作树
    """
    
    def __init__(self, repo_path: Path):
        """
        初始化补丁应用器。
        
        Args:
            repo_path: 仓库根目录路径
        """
        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")
    
    def apply_patch(self,
                   patch_file: Path,
                   create_branch: bool = True) -> Tuple[bool, Optional[str]]:
        """
        应用补丁文件。
        
        Args:
            patch_file: 补丁文件路径（.diff 或 .json 文件）
            create_branch: 是否创建临时分支
            
        Returns:
            (success, error_message)
        """
        try:
            # 如果传入的是 .json 文件，尝试使用对应的 .diff 文件
            if patch_file.suffix == '.json':
                diff_file = patch_file.with_suffix('.diff')
                if diff_file.exists():
                    patch_file = diff_file
                else:
                    # 如果没有 .diff 文件，直接使用区块替换
                    logger.info(f"No .diff file found, using block replacement for {patch_file}")
                    return self._apply_with_block_replacement(patch_file)
            
            # 保存工作树
            self._save_worktree()
            
            # 尝试使用 git apply（仅对 .diff 文件）
            if patch_file.suffix == '.diff':
                # 确保文件存在且使用绝对路径
                patch_file_abs = Path(patch_file).resolve()
                if not patch_file_abs.exists():
                    logger.debug(f"Diff file not found: {patch_file_abs}, using block replacement...")
                else:
                    success, error = self._apply_with_git(patch_file_abs)
                    if success:
                        if create_branch:
                            self._create_temp_branch()
                        return True, None
                    else:
                        # git apply 失败，对于 .diff 文件，不应该回退到 block replacement
                        # 因为 block replacement 需要 JSON 数据，而 .diff 文件没有
                        logger.debug(f"Git apply failed for .diff file: {error}")
                        return False, error
            
            # Fallback: 区块替换（仅对 .json 文件）
            if patch_file.suffix == '.json':
                return self._apply_with_block_replacement(patch_file)
            else:
                # 对于其他格式，返回失败
                return False, f"Unsupported patch file format: {patch_file.suffix}"
        except Exception as e:
            logger.error(f"Error applying patch: {e}")
            return False, str(e)
    
    def _save_worktree(self):
        """保存工作树（git stash 或 checkout）。"""
        try:
            # 尝试 git stash
            result = subprocess.run(
                ['git', 'stash'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                logger.info("Worktree saved with git stash")
        except Exception as e:
            logger.warning(f"Failed to stash: {e}")
    
    def _apply_with_git(self, patch_file: Path) -> Tuple[bool, Optional[str]]:
        """
        使用 git apply 应用补丁。
        
        Args:
            patch_file: 补丁文件路径
            
        Returns:
            (success, error_message)
        """
        try:
            # 确保使用绝对路径
            patch_file_abs = Path(patch_file).resolve()
            
            # 检查文件是否存在
            if not patch_file_abs.exists():
                return False, f"Patch file not found: {patch_file_abs}"
            
            result = subprocess.run(
                ['git', 'apply', '--check', str(patch_file_abs)],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return False, result.stderr.strip() or "Git apply check failed"
            
            # 实际应用
            result = subprocess.run(
                ['git', 'apply', str(patch_file_abs)],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Patch applied successfully with git apply: {patch_file_abs}")
                return True, None
            else:
                return False, result.stderr.strip() or "Git apply failed"
                
        except subprocess.TimeoutExpired:
            return False, "Git apply timeout"
        except FileNotFoundError:
            return False, "Git not found"
        except Exception as e:
            return False, str(e)
    
    def _apply_with_block_replacement(self, patch_file: Path) -> Tuple[bool, Optional[str]]:
        """
        使用区块替换应用补丁（fallback）。
        
        使用代码内容匹配（fuzzy search）来定位代码块，而不是仅依赖行号。
        
        Args:
            patch_file: 补丁文件路径（需要读取 JSON 获取详细信息）
            
        Returns:
            (success, error_message)
        """
        try:
            # 读取补丁 JSON（.json 文件）
            json_file = patch_file.with_suffix('.json')
            if not json_file.exists():
                return False, "Patch JSON file not found"
            
            import json
            with open(json_file, 'r', encoding='utf-8') as f:
                patch_data = json.load(f)
            
            file_path = Path(patch_data['file_path'])
            # 处理绝对路径和相对路径
            if file_path.is_absolute():
                target_file = file_path
            else:
                target_file = self.repo_path / file_path
            
            old_code = patch_data['old_code']
            new_code = patch_data['new_code']
            start_line = patch_data.get('start_line', 0)
            end_line = patch_data.get('end_line', 0)
            
            if not target_file.exists():
                return False, f"File not found: {target_file}"
            
            # 读取文件内容
            content = target_file.read_text(encoding='utf-8')
            lines = content.splitlines(keepends=True)
            
            # 方法1: 尝试使用行号（如果准确）
            if start_line > 0 and end_line > 0 and start_line <= len(lines) and end_line <= len(lines):
                # 验证行号位置的代码是否匹配
                actual_code = ''.join(lines[start_line - 1:end_line])
                # 去除首尾空白进行比较
                if actual_code.strip() == old_code.strip():
                    # 行号准确，直接替换
                    new_lines = lines[:start_line - 1] + [new_code + '\n' if not new_code.endswith('\n') else new_code] + lines[end_line:]
                    new_content = ''.join(new_lines)
                    target_file.write_text(new_content, encoding='utf-8')
                    logger.info(f"Patch applied with block replacement (line-based): {target_file}")
                    return True, None
            
            # 方法2: 使用代码内容匹配（fuzzy search）
            old_code_normalized = old_code.strip()
            content_normalized = content.replace('\r\n', '\n').replace('\r', '\n')
            
            # 查找匹配的代码块
            match_pos = content_normalized.find(old_code_normalized)
            if match_pos >= 0:
                # 找到匹配位置，计算行号
                before_match = content_normalized[:match_pos]
                match_start_line = before_match.count('\n') + 1
                match_end_line = match_start_line + old_code_normalized.count('\n')
                
                # 重新读取文件（使用原始格式）
                lines = content.splitlines(keepends=True)
                if match_start_line <= len(lines) and match_end_line <= len(lines):
                    new_lines = lines[:match_start_line - 1] + [new_code + '\n' if not new_code.endswith('\n') else new_code] + lines[match_end_line:]
                    new_content = ''.join(new_lines)
                    target_file.write_text(new_content, encoding='utf-8')
                    logger.info(f"Patch applied with block replacement (content-based): {target_file} (lines {match_start_line}-{match_end_line})")
                    return True, None
            
            # 方法2.1: 智能匹配 - 提取 old_code 中的关键代码片段进行部分匹配
            # 如果完整匹配失败，尝试提取关键部分进行匹配
            logger.info("Full code match failed, trying partial match...")
            
            # 提取 old_code 中的关键行（去除注释和空行，以及 LLM 可能添加的代码）
            old_code_lines = [line.strip() for line in old_code.split('\n') 
                            if line.strip() 
                            and not line.strip().startswith('//')
                            and 'lottieAnimation' not in line.lower()  # 过滤 LLM 添加的代码
                            and 'Lottie' not in line]  # 过滤 Lottie 相关代码
            
            # 尝试找到这些关键行的连续匹配
            if len(old_code_lines) >= 3:
                # 使用前几行关键代码进行匹配
                key_lines = old_code_lines[:min(5, len(old_code_lines))]
                key_pattern = '\n'.join(key_lines)
                
                match_pos = content_normalized.find(key_pattern)
                if match_pos >= 0:
                    before_match = content_normalized[:match_pos]
                    match_start_line = before_match.count('\n') + 1
                    # 使用原始行号范围作为结束位置
                    if start_line > 0 and end_line > 0:
                        match_end_line = end_line
                    else:
                        match_end_line = match_start_line + len(old_code_lines)
                    
                    lines = content.splitlines(keepends=True)
                    if match_start_line <= len(lines) and match_end_line <= len(lines):
                        new_lines = lines[:match_start_line - 1] + [new_code + '\n' if not new_code.endswith('\n') else new_code] + lines[match_end_line:]
                        new_content = ''.join(new_lines)
                        target_file.write_text(new_content, encoding='utf-8')
                        logger.info(f"Patch applied with block replacement (partial match): {target_file} (lines {match_start_line}-{match_end_line})")
                        return True, None
            
            # 方法2.2: 智能插入模式 - 使用 unified_diff 或提取新增代码
            logger.info("Trying insert mode detection...")
            
            # 优先使用 unified_diff（如果存在且有效）
            unified_diff = patch_data.get('unified_diff', '')
            if unified_diff and unified_diff.strip():
                # 解析 unified_diff 提取新增的行
                diff_lines = unified_diff.split('\n')
                insert_lines = []
                for line in diff_lines:
                    if line.startswith('+') and not line.startswith('+++'):
                        # 提取新增的行（去掉 + 前缀）
                        insert_line = line[1:]  # 去掉开头的 +
                        if insert_line.strip() and not insert_line.strip().startswith('// Add Lottie'):
                            # 跳过一些元数据行
                            if not any(skip in insert_line for skip in ['---', '+++', '@@']):
                                insert_lines.append(insert_line)
                
                if insert_lines:
                    # 找到插入位置（在 Column() { 之后）
                    if start_line > 0 and start_line <= len(lines):
                        # 查找 Column() { 所在的行
                        insert_line = start_line + 1
                        for i in range(start_line, min(start_line + 5, len(lines))):
                            if 'Column() {' in lines[i]:
                                insert_line = i + 1
                                break
                        
                        # 构建要插入的代码（保持原有缩进）
                        insert_code = '\n'.join(insert_lines)
                        if not insert_code.endswith('\n'):
                            insert_code += '\n'
                        
                        # 在指定行之后插入
                        new_lines = lines[:insert_line] + [insert_code] + lines[insert_line:]
                        new_content = ''.join(new_lines)
                        target_file.write_text(new_content, encoding='utf-8')
                        logger.info(f"Patch applied with block replacement (unified_diff insert): {target_file} (inserted at line {insert_line})")
                        return True, None
            
            # 方法2.3: 从 new_code 中提取新增部分（如果 unified_diff 不可用）
            # 比较 new_code 和 old_code，提取新增的代码块
            new_code_lines = new_code.split('\n')
            old_code_lines_list = [line.strip() for line in old_code.split('\n')]
            
            # 智能提取新增代码：找到 new_code 中不在实际文件中的部分
            # 首先，尝试找到 new_code 中第一个不在实际文件中的连续代码块
            insert_code_lines = []
            
            # 方法2.3.1: 查找包含 Lottie 的代码块（通常是新增的）
            lottie_block_start = -1
            lottie_block_end = -1
            for i, new_line in enumerate(new_code_lines):
                if 'Lottie' in new_line or 'lottie' in new_line.lower():
                    if lottie_block_start < 0:
                        lottie_block_start = i
                    lottie_block_end = i + 1
                elif lottie_block_start >= 0:
                    # 检查是否是 Lottie 块的延续（链式调用）
                    line_stripped = new_line.strip()
                    if line_stripped.startswith('.') or line_stripped.startswith(')'):
                        lottie_block_end = i + 1
                    elif line_stripped and not line_stripped.startswith('//'):
                        # 遇到非空非注释行，停止
                        break
            
            if lottie_block_start >= 0:
                # 提取完整的 Lottie 代码块（包括注释）
                # 向前查找注释和空行
                comment_start = max(0, lottie_block_start - 3)
                for i in range(lottie_block_start - 1, max(-1, lottie_block_start - 5), -1):
                    if i >= 0 and i < len(new_code_lines):
                        line = new_code_lines[i].strip()
                        if line.startswith('//') or not line:
                            comment_start = i
                        else:
                            break
                
                # 确保提取到完整的 Lottie 代码块（包括所有链式调用）
                # 继续向后查找，直到遇到非链式调用的行
                while lottie_block_end < len(new_code_lines):
                    line = new_code_lines[lottie_block_end].strip()
                    if not line or line.startswith('//'):
                        lottie_block_end += 1
                    elif line.startswith('.') or (line.startswith(')') and lottie_block_end < len(new_code_lines) - 1):
                        lottie_block_end += 1
                    else:
                        break
                
                insert_code_lines = new_code_lines[comment_start:lottie_block_end]
            
            # 方法2.3.2: 如果没找到 Lottie 块，尝试其他方法
            if not insert_code_lines:
                # 找到 new_code 开头的新增代码（不在 old_code 中的部分）
                for i, new_line in enumerate(new_code_lines):
                    new_line_stripped = new_line.strip()
                    # 跳过空行
                    if not new_line_stripped:
                        continue
                    # 检查是否是新增代码（不在 old_code 中，且包含 Lottie 等关键词）
                    if ('Lottie' in new_line or 'lottie' in new_line.lower()) and \
                       not any(new_line_stripped == old_line.strip() for old_line in old_code_lines_list):
                        insert_code_lines.append(new_line)
                    elif i < 10 and not any(new_line_stripped == old_line.strip() for old_line in old_code_lines_list[:10]):
                        # 前10行中的新增代码
                        insert_code_lines.append(new_line)
                    else:
                        # 如果遇到匹配 old_code 的行，停止提取
                        if any(new_line_stripped == old_line.strip() for old_line in old_code_lines_list):
                            break
            
            if insert_code_lines:
                insert_code = '\n'.join(insert_code_lines)
                if not insert_code.endswith('\n'):
                    insert_code += '\n'
                
                # 查找插入位置：在 Column() { 之后，第一个子元素之前
                if start_line > 0 and start_line <= len(lines):
                    insert_line = start_line + 1
                    # 查找 Column() { 所在的行
                    for i in range(max(0, start_line - 1), min(start_line + 5, len(lines))):
                        if 'Column() {' in lines[i]:
                            # 在 Column() { 之后插入（跳过开括号行）
                            insert_line = i + 1
                            # 跳过可能的空行和注释
                            while insert_line < len(lines) and (not lines[insert_line].strip() or lines[insert_line].strip().startswith('//')):
                                insert_line += 1
                            break
                    
                    new_lines = lines[:insert_line] + [insert_code] + lines[insert_line:]
                    new_content = ''.join(new_lines)
                    target_file.write_text(new_content, encoding='utf-8')
                    logger.info(f"Patch applied with block replacement (new_code extract): {target_file} (inserted at line {insert_line})")
                    return True, None
            
            # 方法3: 如果都失败，尝试使用行号范围进行智能插入
            # 对于添加新代码的情况（new_code 包含 old_code + 新内容），尝试在指定位置插入
            if start_line > 0 and start_line <= len(lines):
                # 检查 new_code 是否包含 old_code 的关键部分
                # 如果是添加新代码的情况，尝试在指定位置插入
                new_code_normalized = new_code.strip()
                
                # 检查是否是"在某个位置插入新代码"的情况
                # 如果 new_code 的前几行与 old_code 匹配，说明是在 old_code 之前插入
                old_code_first_lines = '\n'.join([line.strip() for line in old_code.split('\n')[:3] if line.strip()])
                new_code_first_lines = '\n'.join([line.strip() for line in new_code.split('\n')[:3] if line.strip()])
                
                if old_code_first_lines in new_code_normalized:
                    # 这是插入新代码的情况
                    # 查找插入位置（在 old_code 开始之前）
                    insert_line = start_line
                    # 提取要插入的新代码部分（new_code 中不在 old_code 中的部分）
                    # 简化：如果 new_code 明显比 old_code 长，说明有新增内容
                    if len(new_code) > len(old_code) * 1.2:
                        # 尝试提取新增部分（通常是 new_code 的开头部分）
                        # 查找 new_code 中第一个不在 old_code 中的部分
                        new_part = new_code.split('\n')
                        old_part = old_code.split('\n')
                        
                        # 找到第一个不同的行
                        insert_lines = []
                        for i, new_line in enumerate(new_part):
                            if i < len(old_part) and new_line.strip() == old_part[i].strip():
                                continue
                            if new_line.strip() and not any(new_line.strip() == old_line.strip() for old_line in old_part):
                                insert_lines = new_part[:i+1]
                                break
                        
                        if insert_lines:
                            insert_code = '\n'.join(insert_lines) + '\n'
                            new_lines = lines[:insert_line - 1] + [insert_code] + lines[insert_line - 1:]
                            new_content = ''.join(new_lines)
                            target_file.write_text(new_content, encoding='utf-8')
                            logger.info(f"Patch applied with block replacement (insert mode): {target_file} (inserted at line {insert_line})")
                            return True, None
                
                # 方法3.1: 最后尝试 - 使用行号直接替换（即使内容不完全匹配）
                if end_line > 0 and end_line <= len(lines):
                    logger.warning(f"Code content mismatch, but applying at lines {start_line}-{end_line} anyway (fallback mode)")
                new_lines = lines[:start_line - 1] + [new_code + '\n' if not new_code.endswith('\n') else new_code] + lines[end_line:]
                new_content = ''.join(new_lines)
                target_file.write_text(new_content, encoding='utf-8')
                logger.info(f"Patch applied with block replacement (fallback): {target_file}")
                return True, None
            
            return False, "Could not locate code block to replace"
            
        except Exception as e:
            logger.error(f"Error in block replacement: {e}", exc_info=True)
            return False, str(e)
    
    def _create_temp_branch(self):
        """创建临时分支 autofix/<timestamp>。"""
        try:
            import time
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            branch_name = f"autofix/{timestamp}"
            
            result = subprocess.run(
                ['git', 'checkout', '-b', branch_name],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                logger.info(f"Created temporary branch: {branch_name}")
                
                # git add 修改的文件
                subprocess.run(
                    ['git', 'add', '-A'],
                    cwd=self.repo_path,
                    timeout=10
                )
            else:
                logger.warning(f"Failed to create branch: {result.stderr}")
                
        except Exception as e:
            logger.warning(f"Error creating branch: {e}")

