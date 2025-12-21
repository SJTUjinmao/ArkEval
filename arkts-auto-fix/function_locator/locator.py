"""
Main locator class that orchestrates the entire function location pipeline.
"""

from pathlib import Path
from typing import Optional
import logging

from .config import Config
from .file_scanner import FileScanner
from .file_summarizer import FileSummarizer
from .similarity_search import SimilaritySearch
from .function_extractor import FunctionExtractor
from .llm_filter import LLMFilter
from .output_writer import OutputWriter
from .types.locator_result import LocatorResult
from .types.file_summary import FileSummary
from .types.function_info import FunctionInfo

logger = logging.getLogger(__name__)


class FunctionLocator:
    """Main class that orchestrates the function location pipeline."""
    
    def __init__(self,
                 pangu_model_path: str = None,
                 output_dir: Optional[Path] = None,
                 top_k: int = None):
        """
        Initialize function locator.
        
        Args:
            pangu_model_path: Pangu 模型路径
            output_dir: Output directory for results
            top_k: Number of top-k files to return (default: Config.TOP_K_FILES)
        """
        # Set output directory
        if output_dir:
            Config.set_output_dir(output_dir)
        
        # Set top_k if provided
        if top_k is not None:
            Config.TOP_K_FILES = top_k
        
        # Initialize components
        self.file_scanner = FileScanner()
        pangu_path = pangu_model_path or getattr(Config, 'PANGU_MODEL_PATH', '/opt/pangu/openPangu-Embedded-7B-V1.1')
        self.file_summarizer = FileSummarizer(pangu_model_path=pangu_path)
        self.similarity_search = SimilaritySearch(
            top_k=Config.TOP_K_FILES,
            pangu_model_path=pangu_path
        )
        self.function_extractor = FunctionExtractor()
        self.llm_filter = LLMFilter(
            pangu_model_path=pangu_path,
            context_size=Config.LLM_CONTEXT_SIZE
        )
        self.output_writer = OutputWriter()
    
    def locate(self,
               repo_path: Path,
               problem_statement: str) -> LocatorResult:
        """
        Main method to locate the function that needs modification.
        
        Pipeline:
        1. Scan repository for ArkTS files
        2. Generate summaries for files
        3. Use LLM to select top-k most similar files
        4. Extract functions from matched files
        5. Use LLM to filter and select target function
        6. Return result
        
        Args:
            repo_path: Path to the repository
            problem_statement: Problem statement describing what needs to be fixed
            
        Returns:
            LocatorResult object
        """
        logger.info(f"Starting function location for problem: {problem_statement[:100]}...")
        
        # Step 1: Scan repository
        logger.info("Step 1: Scanning repository for ArkTS files...")
        file_infos = self.file_scanner.scan(repo_path)
        file_infos = self.file_scanner.filter_files(file_infos)
        
        if not file_infos:
            raise ValueError(f"No ArkTS files found in {repo_path}")
        
        logger.info(f"Found {len(file_infos)} ArkTS files")
        
        # Step 2: Generate summaries
        logger.info("Step 2: Generating file summaries...")
        file_summaries = []
        for file_info in file_infos:
            try:
                # file_info 包含: file_name, abs_path, content, file_hash
                file_path = Path(file_info['abs_path'])
                summary = self.file_summarizer.summarize(file_path, file_info.get('content'))
                file_summaries.append(summary)
            except Exception as e:
                logger.warning(f"Error summarizing {file_info.get('abs_path', 'unknown')}: {e}")
                continue
        
        if not file_summaries:
            raise ValueError("No file summaries generated")
        
        logger.info(f"Generated {len(file_summaries)} summaries")
        
        # Step 3: Use LLM to select top-k most similar files
        logger.info("Step 3: Using LLM to select most similar files...")
        # 使用 LLM 选择最相似的文件
        top_files = self.similarity_search.search(problem_statement, file_summaries)
        
        if not top_files:
            raise ValueError("No similar files found")
        
        # 确保至少有 TOP_K_FILES 个不同的文件
        if len(top_files) < Config.TOP_K_FILES:
            logger.warning(f"Only found {len(top_files)} unique files, less than requested {Config.TOP_K_FILES}")
        
        matched_file_paths = [summary.file_path for summary, _ in top_files]
        logger.info(f"Found {len(top_files)} unique similar files")
        
        # Step 4 & 5: Extract functions and filter with LLM
        logger.info("Step 4-5: Extracting functions and filtering with LLM...")
        target_function = None
        all_candidate_functions = []
        reasoning = ""
        code_before = ""
        
        # Process top files in order of similarity
        for file_summary, similarity_score in top_files:
            file_path = Path(file_summary.file_path)
            
            try:
                # Extract functions
                functions = self.function_extractor.extract(file_path)
                all_candidate_functions.extend(functions)
                
                if not functions:
                    continue
                
                # Use LLM to filter
                candidate_decisions = self.llm_filter.filter(
                    problem_statement,
                    file_summary,
                    functions
                )
                
                # 从候选决策中选择第一个需要修改的函数
                for decision in candidate_decisions:
                    if decision.need_modify:
                        target_function = decision.function
                        code_before = decision.original_code
                        reasoning = decision.reason
                        break
                
                if target_function:
                    break
                    
            except Exception as e:
                logger.warning(f"Error processing {file_path}: {e}")
                continue
        
        if not target_function:
            # Fallback: use first function from first file
            if all_candidate_functions:
                target_function = all_candidate_functions[0]
                code_before = target_function.code
                reasoning = "No function selected by LLM, using first candidate"
            else:
                raise ValueError("No functions found in matched files")
        
        # Step 6: Create result
        result = LocatorResult(
            problem_statement=problem_statement,
            target_function=target_function,
            reasoning=reasoning,
            code_before=code_before,
            candidate_functions=all_candidate_functions,
            matched_files=matched_file_paths
        )
        
        logger.info("Function location completed successfully")
        
        return result
    
    def locate_and_save(self,
                        repo_path: Path,
                        problem_statement: str,
                        output_filename: str = "locator_output.json") -> LocatorResult:
        """
        Locate function and save result to file.
        
        Args:
            repo_path: Path to the repository
            problem_statement: Problem statement
            output_filename: Name of output file
            
        Returns:
            LocatorResult object
        """
        result = self.locate(repo_path, problem_statement)
        self.output_writer.write(result, output_filename)
        return result

