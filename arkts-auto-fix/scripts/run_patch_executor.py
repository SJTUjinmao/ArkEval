#!/usr/bin/env python3
"""
CLI 入口 - 运行补丁执行器。

用法：
    python scripts/run_patch_executor.py --repo_path /path/to/repo --patch_file test_output/patches/patch_xxx.json
    python scripts/run_patch_executor.py --repo_path /path/to/repo --patches_dir test_output/patches
"""

import argparse
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from patch_executor import PatchExecutor
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description='应用并验证补丁'
    )
    
    parser.add_argument(
        '--repo_path',
        type=str,
        required=True,
        help='仓库根目录路径'
    )
    
    parser.add_argument(
        '--patch_file',
        type=str,
        default=None,
        help='单个补丁文件路径'
    )
    
    parser.add_argument(
        '--patches_dir',
        type=str,
        default='test_output/patches',
        help='补丁文件目录（默认：test_output/patches）'
    )
    
    parser.add_argument(
        '--no_verify',
        action='store_true',
        help='不验证补丁'
    )
    
    args = parser.parse_args()
    
    # 验证仓库路径
    repo_path = Path(args.repo_path)
    if not repo_path.exists():
        logger.error(f"Repository path does not exist: {repo_path}")
        sys.exit(1)
    
    # 确定补丁文件
    if args.patch_file:
        patch_file = Path(args.patch_file)
        if not patch_file.exists():
            logger.error(f"Patch file not found: {patch_file}")
            sys.exit(1)
        patches_dir = patch_file.parent
    else:
        patch_file = None
        patches_dir = Path(args.patches_dir)
        if not patches_dir.exists():
            logger.error(f"Patches directory not found: {patches_dir}")
            sys.exit(1)
    
    # 创建补丁执行器
    executor = PatchExecutor(repo_path, patches_dir)
    
    # 执行补丁
    logger.info(f"Executing patches in {repo_path}")
    try:
        result = executor.execute(
            patch_file=patch_file,
            verify=not args.no_verify
        )
        
        if result["success"]:
            logger.info("=" * 60)
            logger.info("Patch execution completed successfully!")
            logger.info(f"Applied: {len(result['applied'])} patches")
            logger.info(f"Failed: {len(result['failed'])} patches")
            if result["verified"]:
                verified_count = sum(1 for v in result["verified"] if v["result"]["passed"])
                logger.info(f"Verified: {verified_count}/{len(result['verified'])} patches")
            logger.info("=" * 60)
        else:
            logger.error("Patch execution failed!")
            for failed in result["failed"]:
                logger.error(f"  - {failed['patch']}: {failed['error']}")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Error executing patches: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

