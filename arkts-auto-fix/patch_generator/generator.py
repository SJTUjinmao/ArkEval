"""
补丁生成器主入口 - 基于 function_locator 的定位结果生成代码补丁。

根据模块解析要求：
- 把 function_locator 输出与 problem_statement 送入 LLM
- 要求生成 new_code（函数级替换）或补丁建议
- Prompt 模板需包含明确约束（保持接口不变、最小改动优先、提供 tests_to_run 信息）
"""

from pathlib import Path
from typing import Optional, Dict, Any
import json
import logging
import time

from .llm_patch import LLMPatch
from .patch_formatter import PatchFormatter
from .types.patch import Patch, PatchRequest, PatchResult
from function_locator.config import Config

logger = logging.getLogger(__name__)


class PatchGenerator:
    """
    补丁生成器 - 主入口类。
    
    功能：
        - 读取 locator 输出
        - 调用 LLM 生成补丁
        - 格式化补丁为 unified diff
        - 保存补丁文件
    """
    
    def __init__(self,
                 pangu_model_path: Optional[str] = None,
                 output_dir: Optional[Path] = None,
                 single_file: bool = True):
        """
        初始化补丁生成器。
        
        Args:
            pangu_model_path: Pangu 模型路径
            output_dir: 输出目录
            single_file: 是否将所有补丁保存到一个文件（默认 True）
        """
        self.llm_patch = LLMPatch(
            pangu_model_path=pangu_model_path or getattr(Config, 'PANGU_MODEL_PATH', '/opt/pangu/openPangu-Embedded-7B-V1.1'),
            context_size=Config.LLM_CONTEXT_SIZE
        )
        self.patch_formatter = PatchFormatter()
        self.output_dir = output_dir or Path("test_output/patches")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.single_file = single_file
        self.all_patches = []  # 收集所有补丁 (list[Patch])
    
    def generate(self, locator_output_path: Path) -> PatchResult:
        """
        从 locator 输出生成补丁。
        
        Args:
            locator_output_path: locator 输出 JSON 文件路径
            
        Returns:
            PatchResult: 补丁生成结果
        """
        logger.info(f"Reading locator output from {locator_output_path}")
        
        # 读取 locator 输出
        try:
            with open(locator_output_path, 'r', encoding='utf-8') as f:
                locator_data = json.load(f)
        except Exception as e:
            logger.error(f"Error reading locator output: {e}")
            raise
        
        problem_statement = locator_data.get("problem_statement", "")
        
        # 获取目标函数信息
        target_function = locator_data.get("target_function")
        if not target_function:
            raise ValueError("No target function found in locator output")
        
        file_path = target_function.get("file_path", "")
        target_code = target_function.get("code", "")
        start_line = target_function.get("start_line", 0)
        end_line = target_function.get("end_line", 0)
        
        # 创建补丁请求
        patch_request = PatchRequest(
            problem_statement=problem_statement,
            locator_output_path=locator_output_path,
            target_function_code=target_code,
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            context=json.dumps(locator_data, indent=2)
        )
        
        # 生成补丁
        logger.info("Generating patch using LLM...")
        patch_result = self.llm_patch.generate_patch(patch_request)
        
        if patch_result.error:
            logger.error(f"Patch generation failed: {patch_result.error}")
            return patch_result
        
        # 格式化补丁
        logger.info("Formatting patch as unified diff...")
        unified_diff = self.patch_formatter.format_diff(
            old_code=patch_result.patch.old_code,
            new_code=patch_result.patch.new_code,
            file_path=file_path,
            start_line=patch_result.patch.start_line
        )
        
        patch_result.patch.unified_diff = unified_diff
        
        # 保存补丁
        if self.single_file:
            # 收集补丁，稍后统一保存
            self.all_patches.append(patch_result.patch)
            patch_id = f"patch_{len(self.all_patches)}"
            patch_result.patch.patch_id = patch_id
            logger.info(f"Patch collected (will save to single file): {patch_id}")
        else:
            # 立即保存单个补丁文件
            patch_id = self._save_patch(patch_result.patch)
            patch_result.patch.patch_id = patch_id
            logger.info(f"Patch generated successfully: {patch_id}")
        
        return patch_result
    
    def _save_patch(self, patch: Patch) -> str:
        """
        保存单个补丁到文件。
        
        Args:
            patch: 补丁对象
            
        Returns:
            patch_id: 补丁 ID
        """
        # 生成 patch_id
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        patch_id = f"patch_{timestamp}"
        patch.patch_id = patch_id
        
        # 保存补丁 JSON
        patch_file = self.output_dir / f"{patch_id}.json"
        patch_dict = patch.dict()
        
        with open(patch_file, 'w', encoding='utf-8') as f:
            json.dump(patch_dict, f, indent=2, ensure_ascii=False)
        
        # 保存 unified diff
        diff_file = self.output_dir / f"{patch_id}.diff"
        diff_file.write_text(patch.unified_diff, encoding='utf-8')
        
        logger.info(f"Patch saved to {patch_file} and {diff_file}")
        
        return patch_id
    
    def save_all_patches(self):
        """
        将所有收集的补丁保存到一个文件中。
        
        Returns:
            (json_file, diff_file): 保存的 JSON 和 diff 文件路径
        """
        if not self.all_patches:
            logger.warning("No patches to save")
            return None, None
        
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        
        # 保存所有补丁到一个 JSON 文件
        all_patches_file = self.output_dir / f"all_patches_{timestamp}.json"
        patches_data = {
            "total_patches": len(self.all_patches),
            "generated_at": timestamp,
            "patches": [patch.dict() for patch in self.all_patches]
        }
        
        with open(all_patches_file, 'w', encoding='utf-8') as f:
            json.dump(patches_data, f, indent=2, ensure_ascii=False)
        
        # 合并所有 unified diff 到一个文件
        all_diff_file = self.output_dir / f"all_patches_{timestamp}.diff"
        all_diffs = []
        for i, patch in enumerate(self.all_patches, 1):
            if patch.unified_diff and patch.unified_diff.strip():
                # 添加分隔符和文件信息
                file_name = Path(patch.file_path).name if patch.file_path else f"patch_{i}"
                all_diffs.append(f"\n{'='*80}\n")
                all_diffs.append(f"# Patch {i}: {file_name}\n")
                all_diffs.append(f"# File: {patch.file_path}\n")
                all_diffs.append(f"{'='*80}\n")
                all_diffs.append(patch.unified_diff)
                all_diffs.append("\n")
        
        all_diff_file.write_text(''.join(all_diffs), encoding='utf-8')
        
        logger.info(f"All {len(self.all_patches)} patches saved to {all_patches_file} and {all_diff_file}")
        
        return all_patches_file, all_diff_file
    
    def clear_patches(self):
        """清空收集的补丁列表。"""
        self.all_patches.clear()

