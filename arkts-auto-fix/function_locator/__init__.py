"""
函数定位器包 - 用于在 ArkTS 代码中定位相关函数

这个文件的作用：
1. 把包里的主要功能暴露出来，让外部可以直接使用
2. 提供两个简单的函数：
   - locate_functions(): 根据问题描述，在代码库中找到相关函数
   - save_results_for_patch(): 把结果保存成 JSON 文件，方便后续生成补丁

使用示例：
    from function_locator import locate_functions
    
    # 查找函数
    result = locate_functions("如何实现用户认证？", repo_path="./my_repo")
    
    # 保存结果
    from function_locator import save_results_for_patch
    save_results_for_patch(result, "output.json")
"""

from pathlib import Path
from typing import Dict, Optional

from .locator import FunctionLocator
from .config import Config

# Import types
from .types.file_summary import FileSummary
from .types.function_info import FunctionInfo
from .types.locator_result import LocatorResult, Candidate
from .types.candidate_decision import CandidateDecision

__all__ = [
    'FunctionLocator',
    'Config',
    'FileSummary',
    'FunctionInfo',
    'LocatorResult',
    'Candidate',
    'CandidateDecision',
    'locate_functions',
    'save_results_for_patch',
]

__version__ = '0.1.0'


def locate_functions(
    problem_statement: str,
    repo_path: str = "repos",
    pangu_model_path: Optional[str] = None,
    output_dir: Optional[str] = None
) -> Dict:
    """
    Convenience function to locate functions related to a problem statement.
    
    This is the main public interface for the function locator package.
    It provides a simple way to locate functions without directly instantiating FunctionLocator.
    
    Args:
        problem_statement: Description of the problem to solve
        repo_path: Path to the repository (default: "repos")
        pangu_model_path: Pangu 模型路径 (default: from Config)
        output_dir: Output directory for results (default: from Config)
    
    Returns:
        Dictionary containing the locator result (converted from LocatorResult)
    
    Example:
        >>> result = locate_functions(
        ...     "How to implement user authentication?",
        ...     repo_path="./my_repo",
        ...     pangu_model_path="/opt/pangu/openPangu-Embedded-7B-V1.1"
        ... )
        >>> print(result['target_function']['function_name'])
    """
    # Convert string paths to Path objects
    repo_path_obj = Path(repo_path)
    output_dir_obj = Path(output_dir) if output_dir else None
    
    # Create locator instance
    locator = FunctionLocator(
        pangu_model_path=pangu_model_path,
        output_dir=output_dir_obj
    )
    
    # Execute location
    result = locator.locate(repo_path_obj, problem_statement)
    
    # Convert LocatorResult to dict for backward compatibility
    return result.dict()


def save_results_for_patch(
    result: Dict,
    output_file: Optional[str] = None
) -> Optional[str]:
    """
    Save locator result to JSON file for patch generation.
    
    This function converts the result dictionary to a format suitable for
    patch generation tools.
    
    Args:
        result: Result dictionary from locate_functions() or LocatorResult.dict()
        output_file: Output file path (if None, auto-generates filename)
    
    Returns:
        Path to saved file, or None if saving failed
    
    Example:
        >>> result = locate_functions("Fix timer cleanup", "./repo")
        >>> saved_path = save_results_for_patch(result, "output.json")
    """
    from pathlib import Path
    import json
    import time
    from .config import Config
    
    try:
        # Determine output directory
        output_dir = Config.OUTPUT_DIR or Path("test_output")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename if not provided
        if output_file is None:
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            output_file = f"locator_result_{timestamp}.json"
        
        # Ensure .json extension
        if not output_file.endswith('.json'):
            output_file = f"{output_file}.json"
        
        output_path = output_dir / output_file
        
        # Convert to patch-ready format
        patch_data = {
            "metadata": {
                "problem": result.get("problem_statement", ""),
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
                "version": __version__
            },
            "target_function": result.get("target_function"),
            "reasoning": result.get("reasoning", ""),
            "code_before": result.get("code_before", ""),
            "matched_files": result.get("matched_files", []),
            "candidate_functions": result.get("candidate_functions", [])
        }
        
        # Write to file
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(patch_data, f, ensure_ascii=False, indent=2)
        
        return str(output_path)
        
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to save results: {e}")
        return None

