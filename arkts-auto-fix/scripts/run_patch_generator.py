#!/usr/bin/env python3
"""
CLI 入口 - 运行补丁生成器。

用法：
    python scripts/run_patch_generator.py --locator_output test_output/locator_output.json
"""

import argparse
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from patch_generator import PatchGenerator
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description='生成代码补丁'
    )
    
    parser.add_argument(
        '--locator_output',
        type=str,
        required=True,
        help='Locator 输出 JSON 文件路径'
    )
    
    parser.add_argument(
        '--output_dir',
        type=str,
        default=None,
        help='补丁输出目录（默认：test_output/patches）'
    )
    
    parser.add_argument(
        '--ollama_host',
        type=str,
        default=None,
        help='Ollama API 主机地址'
    )
    
    parser.add_argument(
        '--llm_model',
        type=str,
        default=None,
        help='LLM 模型名称'
    )
    
    args = parser.parse_args()
    
    # 验证输入文件
    locator_output = Path(args.locator_output)
    if not locator_output.exists():
        logger.error(f"Locator output file not found: {locator_output}")
        sys.exit(1)
    
    # 创建补丁生成器
    output_dir = Path(args.output_dir) if args.output_dir else None
    generator = PatchGenerator(
        ollama_host=args.ollama_host,
        llm_model=args.llm_model,
        output_dir=output_dir
    )
    
    # 生成补丁
    logger.info(f"Generating patch from {locator_output}")
    try:
        result = generator.generate(locator_output)
        
        if result.error:
            logger.error(f"Patch generation failed: {result.error}")
            sys.exit(1)
        
        logger.info(f"Patch generated successfully: {result.patch.patch_id}")
        logger.info(f"Patch files saved to: {generator.output_dir}")
        
    except Exception as e:
        logger.error(f"Error generating patch: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

