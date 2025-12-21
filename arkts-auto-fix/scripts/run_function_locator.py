#!/usr/bin/env python3
"""
CLI 入口 - 运行函数定位器。

用法：
    python scripts/run_function_locator.py --repo_path /path/to/repo --problem "问题描述"
    python scripts/run_function_locator.py --repo_path /path/to/repo --problem_file problem.txt --top_k 10
"""

import argparse
import sys
import time
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from function_locator import FunctionLocator, Config
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """主函数。"""
    parser = argparse.ArgumentParser(
        description='运行函数定位器，在代码库中定位相关函数'
    )
    
    parser.add_argument(
        '--repo_path',
        type=str,
        required=True,
        help='仓库根目录路径'
    )
    
    parser.add_argument(
        '--problem',
        type=str,
        help='问题描述文本'
    )
    
    parser.add_argument(
        '--problem_file',
        type=str,
        help='包含问题描述的文件路径'
    )
    
    parser.add_argument(
        '--top_k',
        type=int,
        default=None,
        help='返回的 top-k 文件数量（默认使用配置值）'
    )
    
    parser.add_argument(
        '--output',
        type=str,
        default=None,
        help='输出文件路径（默认：test_output/locator_output.json）'
    )
    
    parser.add_argument(
        '--ollama_host',
        type=str,
        default=None,
        help='Ollama API 主机地址'
    )
    
    parser.add_argument(
        '--embedding_model',
        type=str,
        default=None,
        help='嵌入模型名称'
    )
    
    parser.add_argument(
        '--llm_model',
        type=str,
        default=None,
        help='LLM 模型名称'
    )
    
    args = parser.parse_args()
    
    # 读取问题描述
    if args.problem_file:
        problem_path = Path(args.problem_file)
        if not problem_path.exists():
            logger.error(f"问题文件不存在: {problem_path}")
            sys.exit(1)
        problem_statement = problem_path.read_text(encoding='utf-8').strip()
    elif args.problem:
        problem_statement = args.problem
    else:
        logger.error("必须提供 --problem 或 --problem_file 参数")
        sys.exit(1)
    
    # 验证仓库路径
    repo_path = Path(args.repo_path)
    if not repo_path.exists():
        logger.error(f"仓库路径不存在: {repo_path}")
        sys.exit(1)
    
    # 设置输出目录
    if args.output:
        output_dir = Path(args.output).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        Config.set_output_dir(output_dir)
    
    # 设置 top_k 如果提供了参数
    if args.top_k:
        Config.TOP_K_FILES = args.top_k
        logger.info(f"设置 TOP_K_FILES = {args.top_k}")
    
    # 创建定位器
    logger.info("初始化函数定位器...")
    locator = FunctionLocator(
        ollama_host=args.ollama_host,
        embedding_model=args.embedding_model,
        llm_model=args.llm_model,
        output_dir=Config.OUTPUT_DIR,
        top_k=args.top_k
    )
    
    # 执行定位
    logger.info(f"开始定位函数...")
    logger.info(f"问题描述: {problem_statement[:100]}...")
    logger.info(f"仓库路径: {repo_path}")
    
    start_time = time.time()
    
    try:
        result = locator.locate(repo_path, problem_statement)
        
        elapsed_time = time.time() - start_time
        
        # 保存结果
        output_file = args.output or "locator_output.json"
        locator.output_writer.write(result, output_file)
        
        # 输出统计信息
        logger.info("=" * 60)
        logger.info("定位完成！")
        logger.info(f"耗时: {elapsed_time:.2f} 秒")
        output_path = (Config.OUTPUT_DIR / output_file) if Config.OUTPUT_DIR else Path(output_file)
        logger.info(f"输出文件: {output_path}")
        logger.info(f"匹配文件数: {len(result.matched_files) if result.matched_files else 0}")
        logger.info(f"候选函数数: {len(result.candidate_functions) if result.candidate_functions else 0}")
        
        if result.target_function:
            logger.info(f"目标函数: {result.target_function.function_name}")
            logger.info(f"文件: {result.target_function.file_path}")
            logger.info(f"行号: {result.target_function.start_line}-{result.target_function.end_line}")
        
        logger.info("=" * 60)
        
        # 打印中间缓存路径
        if Config.CACHE_EMBEDDINGS:
            logger.info(f"嵌入向量缓存: {Config.get_embeddings_cache_dir()}")
        if Config.CACHE_SUMMARIES:
            logger.info(f"摘要缓存: {Config.get_summaries_cache_dir()}")
        
    except Exception as e:
        logger.error(f"定位失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

