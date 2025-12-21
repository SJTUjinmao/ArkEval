"""
补丁执行器主控制流程 - 读取补丁、应用、验证、多轮修补循环。

根据模块解析要求：
- 读取 patches/*.json
- 调用 patch_applier 应用
- 调用 verifier 验证
- 负责多轮修补循环（若失败，记录原因并回滚）
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
import logging
import json
import shutil

from .patch_applier import PatchApplier
from .verifier import Verifier

logger = logging.getLogger(__name__)


class PatchExecutor:
    """
    补丁执行器 - 主控制流程。
    
    功能：
        - 读取补丁文件
        - 应用补丁
        - 验证补丁
        - 多轮修补循环
        - 回滚机制
    """
    
    def __init__(self, repo_path: Path, patches_dir: Path):
        """
        初始化补丁执行器。
        
        Args:
            repo_path: 仓库根目录路径
            patches_dir: 补丁文件目录
        """
        self.repo_path = Path(repo_path).resolve()
        self.patches_dir = Path(patches_dir)
        self.patch_applier = PatchApplier(self.repo_path)
        self.verifier = Verifier(self.repo_path)
        self.applied_patches: List[Dict[str, Any]] = []
    
    def execute(self,
               patch_file: Optional[Path] = None,
               verify: bool = True,
               max_retries: int = 3) -> Dict[str, Any]:
        """
        执行补丁。
        
        Args:
            patch_file: 单个补丁文件路径（如果为 None，则执行所有补丁）
            verify: 是否验证补丁
            max_retries: 最大重试次数
            
        Returns:
            执行结果
        """
        if patch_file:
            patch_files = [patch_file]
        else:
            # 查找所有补丁文件
            patch_files = list(self.patches_dir.glob("*.json"))
        
        if not patch_files:
            return {
                "success": False,
                "error": "No patch files found"
            }
        
        results = {
            "success": True,
            "applied": [],
            "failed": [],
            "verified": []
        }
        
        for patch_file in patch_files:
            logger.info(f"Processing patch: {patch_file}")
            
            # 检查是否是合并的补丁文件（包含 total_patches 和 patches 数组）
            import json
            try:
                with open(patch_file, 'r', encoding='utf-8') as f:
                    patch_data = json.load(f)
                
                # 如果是合并的补丁文件格式
                if 'total_patches' in patch_data and 'patches' in patch_data:
                    logger.info(f"Detected merged patch file with {patch_data['total_patches']} patches")
                    
                    # 首先尝试使用合并的 .diff 文件（如果存在）
                    merged_diff_file = patch_file.with_suffix('.diff')
                    if merged_diff_file.exists():
                        logger.info(f"Found merged .diff file: {merged_diff_file}")
                        # 尝试直接使用 git apply 应用整个合并的 .diff 文件
                        success, error = self.patch_applier.apply_patch(merged_diff_file)
                        if success:
                            logger.info("Successfully applied merged .diff file using git apply")
                            # 如果成功，所有补丁都已应用
                            for i, patch in enumerate(patch_data['patches']):
                                results["applied"].append({
                                    "patch_file": str(patch_file),
                                    "patch_index": i,
                                    "file_path": patch.get('file_path', 'unknown'),
                                    "method": "merged_diff_git_apply"
                                })
                            continue
                        else:
                            logger.info(f"Merged .diff file git apply failed: {error}, falling back to individual patches")
                    
                    # 如果合并的 .diff 文件不存在或 git apply 失败，尝试从合并的 .diff 中提取单个补丁
                    patches = patch_data['patches']
                    if merged_diff_file.exists():
                        # 尝试从合并的 .diff 文件中提取每个补丁
                        individual_diffs = self._extract_individual_diffs(merged_diff_file, patches)
                    else:
                        individual_diffs = {}
                        logger.info("No merged .diff file found, will use JSON block replacement")
                    
                    # 为每个补丁创建临时 JSON 和 .diff 文件并应用
                    for i, patch in enumerate(patches):
                        # 创建临时 JSON 文件
                        temp_patch_file = patch_file.parent / f"temp_patch_{i}_{Path(patch.get('file_path', 'unknown')).name}.json"
                        with open(temp_patch_file, 'w', encoding='utf-8') as f:
                            json.dump(patch, f, indent=2, ensure_ascii=False)
                        
                        # 如果从合并的 .diff 中提取到了对应的补丁，创建临时 .diff 文件
                        temp_diff_file = None
                        if i in individual_diffs:
                            temp_diff_file = patch_file.parent / f"temp_patch_{i}_{Path(patch.get('file_path', 'unknown')).name}.diff"
                            with open(temp_diff_file, 'w', encoding='utf-8') as f:
                                f.write(individual_diffs[i])
                            logger.info(f"  Created temporary .diff file for patch {i+1}/{len(patches)}")
                        
                        logger.info(f"  Applying patch {i+1}/{len(patches)}: {Path(patch.get('file_path', 'unknown')).name}")
                        success, error = self.patch_applier.apply_patch(temp_patch_file)
                        
                        # 如果 git apply 失败，删除 .diff 文件并重试（使用 block replacement）
                        if not success and temp_diff_file and temp_diff_file.exists():
                            logger.info(f"  Git apply failed, falling back to block replacement for patch {i+1}")
                            temp_diff_file.unlink()
                            temp_diff_file = None
                            # 重新调用 apply_patch，这次没有 .diff 文件，会使用 block replacement
                            success, error = self.patch_applier.apply_patch(temp_patch_file)
                        
                        # 清理临时文件
                        if temp_patch_file.exists():
                            temp_patch_file.unlink()
                        if temp_diff_file and temp_diff_file.exists():
                            temp_diff_file.unlink()
                        
                        if success:
                            results["applied"].append({
                                "patch_file": str(patch_file),
                                "patch_index": i,
                                "file_path": patch.get('file_path', 'unknown'),
                                "method": "individual_diff" if i in individual_diffs and temp_diff_file else "block_replacement"
                            })
                        else:
                            results["failed"].append({
                                "patch_file": str(patch_file),
                                "patch_index": i,
                                "file_path": patch.get('file_path', 'unknown'),
                                "error": error
                            })
                            results["success"] = False
                    continue
            except Exception as e:
                logger.warning(f"Failed to check patch file format: {e}, treating as single patch")
            
            # 应用单个补丁（传入 .json 文件，applier 内部会处理）
            # applier.apply_patch 会自动处理 .diff 和 .json 文件
            # 直接传入 .json 文件，applier 会尝试使用对应的 .diff 文件，如果失败则使用 block replacement
            success, error = self.patch_applier.apply_patch(patch_file)
            
            if not success:
                results["failed"].append({
                    "patch": str(patch_file),
                    "error": error
                })
                results["success"] = False
                continue
            
            results["applied"].append(str(patch_file))
            
            # 验证补丁
            if verify:
                verify_result = self.verifier.verify(patch_file)
                results["verified"].append({
                    "patch": str(patch_file),
                    "result": verify_result
                })
                
                if not verify_result["passed"]:
                    logger.warning(f"Patch verification failed: {patch_file}")
                    # 可以选择回滚
                    # self._rollback(patch_file)
            
            self.applied_patches.append({
                "patch": str(patch_file),
                "applied": True,
                "verified": verify_result["passed"] if verify else None
            })
        
        return results
    
    def _extract_individual_diffs(self, merged_diff_file: Path, patches: List[Dict]) -> Dict[int, str]:
        """
        从合并的 .diff 文件中提取每个补丁对应的部分，或从 JSON 中重新生成。
        
        Args:
            merged_diff_file: 合并的 .diff 文件路径
            patches: 补丁列表
            
        Returns:
            字典，键为补丁索引，值为对应的 diff 内容
        """
        individual_diffs = {}
        
        # 优先尝试从 JSON 中重新生成正确的 diff（使用 start_line 信息）
        for i, patch in enumerate(patches):
            try:
                # 从 JSON 中获取信息
                old_code = patch.get('old_code', '')
                new_code = patch.get('new_code', '')
                file_path = patch.get('file_path', '')
                start_line = patch.get('start_line', 1)
                
                if not old_code or not new_code or not file_path:
                    continue
                
                # 获取文件相对路径
                try:
                    abs_path = Path(file_path)
                    if abs_path.is_absolute():
                        rel_path = abs_path.relative_to(self.repo_path)
                    else:
                        rel_path = Path(file_path)
                    file_rel_path = str(rel_path).replace('\\', '/')
                except Exception:
                    file_rel_path = Path(file_path).name
                
                # 使用 PatchFormatter 生成正确的 diff
                from patch_generator.patch_formatter import PatchFormatter
                formatter = PatchFormatter(use_git=False)  # 使用 difflib，更可靠
                diff_text = formatter.format_diff(
                    old_code=old_code,
                    new_code=new_code,
                    file_path=file_rel_path,
                    start_line=start_line,
                    context_lines=3
                )
                
                if diff_text and diff_text.strip():
                    # 修正文件路径（确保使用相对路径）
                    diff_lines = diff_text.split('\n')
                    corrected_lines = []
                    for line in diff_lines:
                        if line.startswith('--- a/') or line.startswith('+++ b/'):
                            # 替换为正确的相对路径
                            if line.startswith('--- a/'):
                                corrected_lines.append('--- a/' + file_rel_path)
                            else:
                                corrected_lines.append('+++ b/' + file_rel_path)
                        else:
                            corrected_lines.append(line)
                    
                    individual_diffs[i] = '\n'.join(corrected_lines)
                    logger.debug(f"Generated diff for patch {i+1} from JSON ({Path(file_path).name})")
            except Exception as e:
                logger.debug(f"Failed to generate diff from JSON for patch {i+1}: {e}")
                continue
        
        # 如果从 JSON 生成成功，直接返回
        if individual_diffs:
            return individual_diffs
        
        # 否则，尝试从合并的 .diff 文件中提取（fallback）
        try:
            with open(merged_diff_file, 'r', encoding='utf-8') as f:
                diff_content = f.read()
            
            # 按分隔符分割补丁（分隔符是 "================================================================================"）
            patch_sections = diff_content.split('================================================================================')
            
            # 跳过第一个空部分（文件开头可能是空行）
            if len(patch_sections) > 1:
                patch_sections = patch_sections[1:]
            
            # 为每个补丁查找对应的 section
            for i, patch in enumerate(patches):
                if i in individual_diffs:
                    continue  # 已经成功生成，跳过
                
                file_path = patch.get('file_path', '')
                # 获取文件相对路径（用于 diff 文件路径）
                if file_path:
                    try:
                        abs_path = Path(file_path)
                        if abs_path.is_absolute():
                            rel_path = abs_path.relative_to(self.repo_path)
                        else:
                            rel_path = Path(file_path)
                        file_rel_path = str(rel_path).replace('\\', '/')
                    except Exception:
                        file_rel_path = Path(file_path).name
                else:
                    file_rel_path = 'unknown'
                
                file_name = Path(file_path).name if file_path else ''
                
                # 查找包含该文件名的 section
                for section_idx, section in enumerate(patch_sections):
                    # 检查 section 是否包含该文件的补丁
                    # 格式通常是: "# Patch X: FileName.ets" 或 "--- a/FileName.ets" 或 "+++ b/FileName.ets"
                    patch_number_match = f"# Patch {i+1}:" in section or f"Patch {i+1}:" in section
                    file_match = file_name and (f"--- a/{file_name}" in section or f"+++ b/{file_name}" in section)
                    
                    if patch_number_match or file_match:
                        # 找到匹配的 section，提取完整的 diff 内容
                        section_lines = section.split('\n')
                        cleaned_lines = []
                        found_diff_start = False
                        
                        for line_idx, line in enumerate(section_lines):
                            # 跳过注释行（以 # 开头的行）
                            if line.strip().startswith('#'):
                                continue
                            
                            # 检查是否是合并的 diff 行（包含 ---、+++ 和 @@ 在同一行）
                            if '--- a/' in line and '+++ b/' in line and '@@' in line:
                                # 分离合并的 diff 头部
                                # 格式: --- a/File+++ b/File@@ -1,5 +1,14 @@ code...
                                # 需要分离为三行：--- a/File, +++ b/File, @@ -1,5 +1,14 @@
                                try:
                                    # 提取文件名（假设文件名在 --- a/ 和 +++ b/ 之间）
                                    if '+++ b/' in line:
                                        parts = line.split('+++ b/')
                                        if len(parts) == 2:
                                            # 第一部分：--- a/FileName
                                            old_part = parts[0]  # '--- a/PhoneNumber.ets'
                                            # 第二部分：FileName@@ -1,5 +1,14 @@ code...
                                            new_part = '+++ b/' + parts[1]  # '+++ b/PhoneNumber.ets@@ -1,5 +1,14 @@ code...'
                                            
                                            # 分离文件名和 @@ 部分
                                            if '@@' in new_part:
                                                at_index = new_part.index('@@')
                                                file_name = new_part[6:at_index].strip()  # 跳过 '+++ b/'
                                                hunk_and_code = new_part[at_index:]  # '@@ -1,5 +1,14 @@ code...'
                                                
                                                # 分离 hunk 头部和代码
                                                # hunk 格式: @@ -1,5 +1,14 @@
                                                hunk_end = hunk_and_code.find('@@', 2)
                                                if hunk_end > 0:
                                                    hunk = hunk_and_code[:hunk_end + 2]  # '@@ -1,5 +1,14 @@'
                                                    code_part = hunk_and_code[hunk_end + 2:]  # '     code...' (保留前导空格)
                                                    
                                                    # 使用之前计算的相对路径
                                                    # 构建正确的 diff 格式
                                                    cleaned_lines.append('--- a/' + file_rel_path)  # '--- a/features/.../PhoneNumber.ets'
                                                    cleaned_lines.append('+++ b/' + file_rel_path)  # '+++ b/features/.../PhoneNumber.ets'
                                                    cleaned_lines.append(hunk)  # '@@ -1,5 +1,14 @@'
                                                    if code_part:
                                                        cleaned_lines.append(code_part)  # '     code...' (保留前导空格)
                                                    
                                                    # 继续读取后续的代码行，直到遇到下一个分隔符
                                                    for next_line in section_lines[line_idx + 1:]:
                                                        if '================================================================================' in next_line:
                                                            break
                                                        # 保留所有行（包括空行，因为空行在 diff 中也有意义）
                                                        cleaned_lines.append(next_line)
                                                    
                                                    found_diff_start = True
                                                    break  # 找到匹配的 section，跳出内层循环
                                except Exception as e:
                                    logger.debug(f"Failed to parse merged diff line: {e}")
                                    # 如果解析失败，跳过这一行
                                    continue
                            
                            # 找到 diff 开始标记（标准格式）
                            if line.startswith('--- ') or line.startswith('+++ '):
                                found_diff_start = True
                                cleaned_lines.append(line)
                            elif found_diff_start:
                                # 在找到 diff 开始后，保留所有行（包括空行，直到下一个分隔符）
                                if '================================================================================' in line:
                                    break
                                cleaned_lines.append(line)
                        
                        if cleaned_lines:
                            # 确保有 diff 头部
                            diff_text = '\n'.join(cleaned_lines).strip()
                            if diff_text and ('--- ' in diff_text or '@@' in diff_text):
                                individual_diffs[i] = diff_text
                                logger.debug(f"Extracted diff for patch {i+1} ({file_name})")
                                break
        except Exception as e:
            logger.warning(f"Failed to extract individual diffs from merged file: {e}")
        
        return individual_diffs
    
    def _rollback(self, patch_file: Path):
        """
        回滚补丁。
        
        Args:
            patch_file: 要回滚的补丁文件
        """
        try:
            logger.info(f"Rolling back patch: {patch_file}")
            # 使用 git reset 回滚
            import subprocess
            subprocess.run(
                ['git', 'reset', '--hard', 'HEAD'],
                cwd=self.repo_path,
                timeout=10
            )
            logger.info("Patch rolled back successfully")
        except Exception as e:
            logger.error(f"Error rolling back patch: {e}")

