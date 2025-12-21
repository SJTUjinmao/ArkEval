#!/usr/bin/env python3
"""
完整工作流脚本 - 从 function_locator 到 patch_generator 到 patch_executor。

工作流程：
1. 使用 function_locator 定位需要修改的函数
2. 使用 patch_generator 生成补丁
3. 使用 patch_executor 应用补丁（可选）

用法:
    python run_full_workflow.py --repo <repo_path> --problem "<problem_statement>" [--apply]
"""

import sys
import argparse
import logging
import json
from pathlib import Path
from typing import Optional
import importlib

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 强制重新加载模块（解决缓存问题）
# 注意：必须在导入之前清理，否则可能已经加载了旧版本
modules_to_remove = [
    'patch_generator',
    'patch_generator.generator',
    'patch_generator.llm_patch',
    'patch_generator.__init__',
    'function_locator',
    'function_locator.locator',
    'function_locator.__init__',
]

for mod_name in list(sys.modules.keys()):
    if any(mod_name.startswith(m) for m in modules_to_remove):
        del sys.modules[mod_name]

from function_locator import FunctionLocator, Config
from patch_generator import PatchGenerator
from patch_executor import PatchExecutor

# 配置日志
test_output_dir = Path("test_output")
test_output_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(test_output_dir / 'workflow.log', encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)


class FullWorkflow:
    """完整工作流 - 从定位到生成到应用补丁。"""
    
    def __init__(self,
                 repo_path: Path,
                 output_dir: Optional[Path] = None,
                 pangu_model_path: str = "/opt/pangu/openPangu-Embedded-7B-V1.1"):
        """
        初始化工作流。
        
        Args:
            repo_path: 仓库路径
            output_dir: 输出目录
            pangu_model_path: Pangu 模型路径
        """
        self.repo_path = Path(repo_path).resolve()
        self.output_dir = output_dir or Path("test_output")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 设置配置
        Config.set_output_dir(self.output_dir)
        
        # 初始化各模块
        # 从环境变量获取 top_k（如果设置了）
        top_k = None
        import os
        if 'FUNCTION_LOCATOR_TOP_K' in os.environ:
            try:
                top_k = int(os.environ['FUNCTION_LOCATOR_TOP_K'])
            except ValueError:
                pass
        
        self.locator = FunctionLocator(
            pangu_model_path=pangu_model_path,
            output_dir=self.output_dir, 
            top_k=top_k
        )
        self.patch_generator = PatchGenerator(
            pangu_model_path=pangu_model_path,
            output_dir=self.output_dir / "patches",
            single_file=True  # 将所有补丁保存到一个文件
        )
        
        self.patches_dir = self.output_dir / "patches"
        self.patches_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Workflow initialized:")
        logger.info(f"  Repository: {self.repo_path}")
        logger.info(f"  Output directory: {self.output_dir}")
    
    def run(self,
            problem_statement: str,
            apply_patch: bool = False,
            verify: bool = True) -> dict:
        """
        运行完整工作流。
        
        Args:
            problem_statement: 问题描述
            apply_patch: 是否应用补丁
            verify: 是否验证补丁
            
        Returns:
            工作流执行结果
        """
        results = {
            "success": False,
            "steps": {},
            "locator_output": None,
            "patch_result": None,
            "executor_result": None
        }
        
        logger.info("=" * 80)
        logger.info("开始完整工作流")
        logger.info("=" * 80)
        logger.info(f"问题描述: {problem_statement}")
        logger.info(f"仓库路径: {self.repo_path}")
        logger.info("")
        
        # 步骤 1: 函数定位
        logger.info("-" * 80)
        logger.info("步骤 1: 函数定位 (Function Locator)")
        logger.info("-" * 80)
        
        try:
            locator_result = self.locator.locate(self.repo_path, problem_statement)
            
            if not locator_result.target_function:
                logger.error("未找到目标函数")
                results["steps"]["locator"] = {
                    "success": False,
                    "error": "No target function found"
                }
                return results
            
            # 保存定位结果
            locator_output_file = self.output_dir / "locator_output.json"
            self.locator.output_writer.write(locator_result, "locator_output.json")
            
            results["steps"]["locator"] = {
                "success": True,
                "output_file": str(locator_output_file),
                "target_function": locator_result.target_function.function_name,
                "file_path": locator_result.target_function.file_path,
                "matched_files": len(locator_result.matched_files) if locator_result.matched_files else 0
            }
            results["locator_output"] = locator_output_file
            
            logger.info(f"✓ 定位成功: {locator_result.target_function.function_name}")
            logger.info(f"  文件: {locator_result.target_function.file_path}")
            logger.info(f"  行号: {locator_result.target_function.start_line}-{locator_result.target_function.end_line}")
            logger.info(f"  匹配文件数: {len(locator_result.matched_files) if locator_result.matched_files else 0}")
            
        except Exception as e:
            logger.error(f"✗ 定位失败: {e}", exc_info=True)
            results["steps"]["locator"] = {
                "success": False,
                "error": str(e)
            }
            return results
        
        # 步骤 2: 补丁生成（为多个文件生成补丁）
        logger.info("")
        logger.info("-" * 80)
        logger.info("步骤 2: 补丁生成 (Patch Generator)")
        logger.info("-" * 80)
        
        patch_results = []
        patch_files = []
        
        # 为匹配的文件生成补丁
        matched_files = locator_result.matched_files or []
        if not matched_files:
            matched_files = [locator_result.target_function.file_path] if locator_result.target_function else []
        
        logger.info(f"为 {len(matched_files)} 个匹配文件生成补丁...")
        
        for file_path in matched_files:
            file_path_obj = Path(file_path)
            if not file_path_obj.exists():
                logger.warning(f"文件不存在，跳过: {file_path}")
                continue
            
            try:
                # 为每个文件生成补丁
                logger.info(f"  处理文件: {file_path_obj.name}")
                
                # 创建临时的 locator 输出，只包含当前文件
                from function_locator.function_extractor import FunctionExtractor
                extractor = FunctionExtractor()
                functions = extractor.extract(file_path_obj)
                
                if not functions:
                    logger.warning(f"  文件 {file_path_obj.name} 没有提取到函数，跳过")
                    continue
                
                # 使用第一个函数作为目标（或者可以改进为选择最相关的函数）
                target_func = functions[0]
                
                # 创建补丁请求
                from patch_generator.types.patch import PatchRequest
                patch_request = PatchRequest(
                    problem_statement=problem_statement,
                    locator_output_path=locator_output_file,
                    target_function_code=target_func.code,
                    file_path=str(file_path),
                    start_line=target_func.start_line,
                    end_line=target_func.end_line,
                    context=json.dumps({
                        "problem_statement": problem_statement,
                        "file_path": str(file_path),
                        "functions": [f.function_name for f in functions]
                    }, indent=2)
                )
                
                # 生成补丁
                patch_result = self.patch_generator.llm_patch.generate_patch(patch_request)
                
                if patch_result.error:
                    logger.warning(f"  文件 {file_path_obj.name} 补丁生成失败: {patch_result.error}")
                    continue
                
                # 格式化并保存补丁
                from patch_generator.patch_formatter import PatchFormatter
                formatter = PatchFormatter()
                unified_diff = formatter.format_diff(
                    old_code=patch_result.patch.old_code,
                    new_code=patch_result.patch.new_code,
                    file_path=str(file_path),
                    start_line=patch_result.patch.start_line
                )
                patch_result.patch.unified_diff = unified_diff
                patch_result.patch.file_path = str(file_path)
                
                # 保存补丁（如果使用单文件模式，会收集到 all_patches 中）
                if self.patch_generator.single_file:
                    # 收集补丁，稍后统一保存
                    self.patch_generator.all_patches.append(patch_result.patch)
                    patch_id = f"patch_{len(self.patch_generator.all_patches)}"
                    patch_result.patch.patch_id = patch_id
                else:
                    # 立即保存单个补丁文件
                    patch_id = self.patch_generator._save_patch(patch_result.patch)
                    patch_result.patch.patch_id = patch_id
                    patch_files.append(self.patches_dir / f"{patch_id}.json")
                
                patch_results.append(patch_result)
                
                logger.info(f"  ✓ 补丁生成成功: {patch_id}")
                logger.info(f"    文件: {file_path_obj.name}")
                logger.info(f"    行号: {patch_result.patch.start_line}-{patch_result.patch.end_line}")
                
            except Exception as e:
                logger.warning(f"  文件 {file_path_obj.name} 处理失败: {e}")
                continue
        
        if not patch_results:
            logger.error("✗ 没有成功生成任何补丁")
            results["steps"]["patch_generator"] = {
                "success": False,
                "error": "No patches generated"
            }
            return results
        
        # 如果使用单文件模式，保存所有补丁到一个文件
        if self.patch_generator.single_file and patch_results:
            all_patches_json, all_patches_diff = self.patch_generator.save_all_patches()
            if all_patches_json:
                logger.info(f"✓ 所有补丁已保存到: {all_patches_json.name} 和 {all_patches_diff.name}")
                patch_files = [all_patches_json, all_patches_diff]
        
        results["steps"]["patch_generator"] = {
            "success": True,
            "patches_count": len(patch_results),
            "patch_files": [str(f) for f in patch_files]
        }
        results["patch_result"] = patch_results[0] if patch_results else None
        results["patch_results"] = patch_results
        
        logger.info(f"✓ 总共生成了 {len(patch_results)} 个补丁")
        
        # 步骤 3: 补丁应用（可选）
        if apply_patch:
            logger.info("")
            logger.info("-" * 80)
            logger.info("步骤 3: 补丁应用 (Patch Executor)")
            logger.info("-" * 80)
            
            try:
                executor = PatchExecutor(
                    repo_path=self.repo_path,
                    patches_dir=self.patches_dir
                )
                
                # 获取刚生成的补丁文件路径
                # 查找最新生成的补丁文件
                latest_patch_file = None
                if self.patch_generator.all_patches:
                    # 查找最新的 all_patches_*.json 文件
                    patch_files = sorted(self.patches_dir.glob("all_patches_*.json"), reverse=True)
                    if patch_files:
                        latest_patch_file = patch_files[0]
                        logger.info(f"应用最新补丁文件: {latest_patch_file.name}")
                
                # 应用补丁（只应用最新生成的补丁文件）
                executor_result = executor.execute(
                    patch_file=latest_patch_file,  # 只应用最新的补丁文件
                    verify=verify,
                    max_retries=3
                )
                
                results["steps"]["patch_executor"] = {
                    "success": executor_result["success"],
                    "applied": executor_result["applied"],
                    "failed": executor_result["failed"],
                    "verified": executor_result["verified"]
                }
                results["executor_result"] = executor_result
                
                if executor_result["success"]:
                    logger.info(f"✓ 补丁应用成功")
                    logger.info(f"  已应用: {len(executor_result['applied'])} 个补丁")
                    if executor_result["verified"]:
                        passed = sum(1 for v in executor_result["verified"] if v["result"]["passed"])
                        logger.info(f"  验证通过: {passed}/{len(executor_result['verified'])}")
                else:
                    logger.error(f"✗ 补丁应用失败")
                    for failure in executor_result["failed"]:
                        # 兼容不同的键名格式
                        patch_name = failure.get('patch') or failure.get('patch_file') or failure.get('file_path', 'unknown')
                        logger.error(f"  - {patch_name}: {failure['error']}")
                
            except Exception as e:
                logger.error(f"✗ 补丁应用失败: {e}", exc_info=True)
                results["steps"]["patch_executor"] = {
                    "success": False,
                    "error": str(e)
                }
        else:
            logger.info("")
            logger.info("跳过补丁应用步骤（使用 --apply 参数启用）")
            results["steps"]["patch_executor"] = {
                "success": None,
                "skipped": True
            }
        
        # 总结
        logger.info("")
        logger.info("=" * 80)
        logger.info("工作流完成")
        logger.info("=" * 80)
        
        all_success = all(
            step.get("success") is not False 
            for step in results["steps"].values()
        )
        results["success"] = all_success
        
        logger.info(f"工作流状态: {'✓ 成功' if results['success'] else '✗ 失败'}")
        logger.info(f"输出目录: {self.output_dir}")
        
        return results


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="完整工作流：从函数定位到补丁生成到补丁应用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 仅定位和生成补丁
  python run_full_workflow.py --repo /path/to/repo --problem "修复某个bug"
  
  # 完整流程（包括应用补丁）
  python run_full_workflow.py --repo /path/to/repo --problem "修复某个bug" --apply
        """
    )
    
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="仓库路径"
    )
    
    parser.add_argument(
        "--problem",
        type=str,
        required=True,
        help="问题描述"
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出目录（默认: test_output）"
    )
    
    parser.add_argument(
        "--apply",
        action="store_true",
        help="应用补丁（默认: 仅生成补丁）"
    )
    
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="不验证补丁（仅在 --apply 时有效）"
    )
    
    parser.add_argument(
        "--pangu-model-path",
        type=str,
        default="/opt/pangu/openPangu-Embedded-7B-V1.1",
        help="Pangu 模型路径（默认: /opt/pangu/openPangu-Embedded-7B-V1.1）"
    )
    
    args = parser.parse_args()
    
    # 创建工作流
    workflow = FullWorkflow(
        repo_path=Path(args.repo),
        output_dir=Path(args.output) if args.output else None,
        pangu_model_path=args.pangu_model_path
    )
    
    # 运行工作流
    results = workflow.run(
        problem_statement=args.problem,
        apply_patch=args.apply,
        verify=not args.no_verify
    )
    
    # 返回退出码
    sys.exit(0 if results["success"] else 1)


if __name__ == '__main__':
    main()

