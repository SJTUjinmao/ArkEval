"""
LLM 过滤器 - 在 top-k 文件/函数候选基础上进行快速判断/精炼或 rerank。

根据模块解析要求：
- 支持两种模式：light_rerank（仅选择/评分）或 detailed_locate（返回更加精细位置/建议）
- 为控制延迟，构造最小 prompt（只包含必要字段）
- Prompt 输出应标准化为 JSON 或简单标签（yes/no + index）便于解析
"""

from typing import List, Optional
try:
    from typing import Literal
except ImportError:
    # Python < 3.8
    from typing_extensions import Literal
import logging
import json
from pathlib import Path
import time
import sys
from pathlib import Path as PathLib

# 添加项目根目录到路径
project_root = PathLib(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from pangu_model import PanguModel

from .types.function_info import FunctionInfo
from .types.file_summary import FileSummary
from .types.candidate_decision import CandidateDecision
from .config import Config

logger = logging.getLogger(__name__)


class LLMFilter:
    """
    LLM 过滤器 - 使用 LLM 对函数候选进行过滤和排序。
    
    支持两种模式：
        - light_rerank: 快速重排序，仅选择/评分（默认，延迟低）
        - detailed_locate: 详细定位，返回精确位置和修改建议（延迟较高）
    """
    
    def __init__(self,
                 pangu_model_path: str = None,
                 temperature: float = None,
                 max_tokens: int = None,
                 context_size: int = None,
                 mode: Literal["light_rerank", "detailed_locate"] = "light_rerank",
                 audit_log_dir: Optional[Path] = None):
        """
        初始化 LLM 过滤器。
        
        Args:
            pangu_model_path: Pangu 模型路径
            temperature: 生成温度
            max_tokens: 最大生成 token 数
            context_size: 上下文窗口大小（num_ctx）
            mode: 过滤模式（"light_rerank" 或 "detailed_locate"）
            audit_log_dir: 审计日志目录（用于记录 LLM 输入输出）
        """
        self.pangu_model_path = pangu_model_path or getattr(Config, 'PANGU_MODEL_PATH', '/opt/pangu/openPangu-Embedded-7B-V1.1')
        self.temperature = temperature if temperature is not None else Config.LLM_TEMPERATURE
        self.max_tokens = max_tokens or Config.LLM_MAX_TOKENS
        self.context_size = context_size if context_size is not None else Config.LLM_CONTEXT_SIZE
        self.mode = mode
        
        # 初始化 Pangu 模型
        try:
            self.pangu_model = PanguModel(model_path=self.pangu_model_path)
        except Exception as e:
            logger.error(f"初始化 Pangu 模型失败: {e}")
            raise
        
        # 只有在启用审计日志时才创建目录
        if Config.ENABLE_AUDIT_LOGS:
            self.audit_log_dir = audit_log_dir or (Config.OUTPUT_DIR / "llm_audit_logs" if Config.OUTPUT_DIR else None)
            if self.audit_log_dir:
                self.audit_log_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.audit_log_dir = None
    
    def filter(self,
               problem_statement: str,
               file_summary: FileSummary,
               functions: List[FunctionInfo]) -> List[CandidateDecision]:
        """
        过滤函数，返回候选决策列表。
        
        Args:
            problem_statement: 问题描述
            file_summary: 文件摘要
            functions: 函数列表
            
        Returns:
            List[CandidateDecision]: 候选决策列表，按相关性排序
        """
        if not functions:
            logger.warning("No functions provided for filtering")
            return []
        
        # 根据模式构建 prompt
        if self.mode == "light_rerank":
            prompt = self._build_light_rerank_prompt(problem_statement, file_summary, functions)
        else:
            prompt = self._build_detailed_locate_prompt(problem_statement, file_summary, functions)
        
        # 记录审计日志
        if self.audit_log_dir:
            self._log_audit("input", problem_statement, file_summary, functions, prompt)
        
        # 调用 LLM
        try:
            response = self._call_llm(prompt)
            
            # 记录响应
            if self.audit_log_dir:
                self._log_audit("output", problem_statement, file_summary, functions, response)
            
            # 解析响应
            if self.mode == "light_rerank":
                decisions = self._parse_light_rerank_response(response, functions, file_summary)
            else:
                decisions = self._parse_detailed_locate_response(response, functions, file_summary)
            
            return decisions
            
        except Exception as e:
            logger.error(f"Error calling LLM filter: {e}")
            # Fallback: 返回所有函数作为候选
            return self._create_fallback_decisions(functions, file_summary)
    
    def _build_light_rerank_prompt(self,
                                  problem_statement: str,
                                  file_summary: FileSummary,
                                  functions: List[FunctionInfo]) -> str:
        """
        构建轻量级重排序 prompt（最小 prompt，只包含必要字段）。
        
        Args:
            problem_statement: 问题描述
            file_summary: 文件摘要
            functions: 函数列表
            
        Returns:
            Formatted prompt string
        """
        # 最小 prompt - 只包含必要信息
        prompt_parts = [
            "Problem:", problem_statement[:200],  # 限制长度
            f"File: {file_summary.file_name}",
            "",
            "Functions:"
        ]
        
        # 只包含函数名和关键信息
        for i, func in enumerate(functions, 1):
            # 限制代码长度
            code_preview = func.code[:300] + "..." if len(func.code) > 300 else func.code
            prompt_parts.append(f"{i}. {func.function_name} (lines {func.start_line}-{func.end_line})")
            prompt_parts.append(code_preview)
        
        prompt_parts.extend([
            "",
            "Respond with JSON array, each item:",
            '{"index": <number>, "need_modify": true/false, "score": 0.0-1.0}',
            "Sort by score descending."
        ])
        
        return "\n".join(prompt_parts)
    
    def _build_detailed_locate_prompt(self,
                                     problem_statement: str,
                                     file_summary: FileSummary,
                                     functions: List[FunctionInfo]) -> str:
        """
        构建详细定位 prompt（包含更多信息，返回精确位置和建议）。
        
        Args:
            problem_statement: 问题描述
            file_summary: 文件摘要
            functions: 函数列表
            
        Returns:
            Formatted prompt string
        """
        prompt_parts = [
            "Analyze ArkTS code to identify functions needing modification.",
            "",
            "Problem:", problem_statement,
            "",
            f"File: {file_summary.file_name}",
            f"Summary: {file_summary.summary}",
            "",
            "Functions:"
        ]
        
        for i, func in enumerate(functions, 1):
            prompt_parts.append(f"\n{i}. {func.function_name}")
            prompt_parts.append(f"   Lines: {func.start_line}-{func.end_line}")
            prompt_parts.append(f"   Code:\n{func.code}")
        
        prompt_parts.extend([
            "",
            "Respond with JSON array:",
            '[{"need_modify": true/false, "reason": "...", "function_name": "...",',
            '  "start_line": <number>, "end_line": <number>, "original_code": "...",',
            '  "modified_hint": "...", "score": 0.0-1.0}]',
            "Include all functions, sort by score descending."
        ])
        
        return "\n".join(prompt_parts)
    
    def _call_llm(self, prompt: str) -> str:
        """
        Call LLM via Pangu model.
        
        Args:
            prompt: Prompt to send to LLM
            
        Returns:
            LLM response text
        """
        try:
            response = self.pangu_model.generate(
                prompt=prompt,
                max_new_tokens=self.max_tokens,
                temperature=self.temperature,
                max_length=self.context_size
            )
            return response
        except Exception as e:
            logger.error(f"Pangu 模型调用失败: {e}")
            raise
    
    def _parse_light_rerank_response(self,
                                    response: str,
                                    functions: List[FunctionInfo],
                                    file_summary: FileSummary) -> List[CandidateDecision]:
        """
        解析轻量级重排序响应。
        
        Args:
            response: LLM 响应文本
            functions: 函数列表
            file_summary: 文件摘要
            
        Returns:
            CandidateDecision 列表
        """
        decisions = []
        
        try:
            # 提取 JSON
            json_start = response.find('[')
            json_end = response.rfind(']') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = response[json_start:json_end]
                data_list = json.loads(json_str)
                
                # 创建决策对象
                for item in data_list:
                    index = item.get("index", 0) - 1  # 转换为 0-based
                    if 0 <= index < len(functions):
                        func = functions[index]
                        decisions.append(CandidateDecision(
                            need_modify=item.get("need_modify", False),
                            reason=f"LLM rerank score: {item.get('score', 0.0):.2f}",
                            function=func,
                            start_line=func.start_line,
                            end_line=func.end_line,
                            original_code=func.code,
                            score=item.get("score", 0.0)
                        ))
                
                # 按分数排序
                decisions.sort(key=lambda x: x.score or 0.0, reverse=True)
                
        except Exception as e:
            logger.warning(f"Error parsing light rerank response: {e}")
            # Fallback
            return self._create_fallback_decisions(functions, file_summary)
        
        return decisions if decisions else self._create_fallback_decisions(functions, file_summary)
    
    def _parse_detailed_locate_response(self,
                                       response: str,
                                       functions: List[FunctionInfo],
                                       file_summary: FileSummary) -> List[CandidateDecision]:
        """
        解析详细定位响应。
        
        Args:
            response: LLM 响应文本
            functions: 函数列表
            file_summary: 文件摘要
            
        Returns:
            CandidateDecision 列表
        """
        decisions = []
        
        try:
            # 提取 JSON 数组
            json_start = response.find('[')
            json_end = response.rfind(']') + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = response[json_start:json_end]
                data_list = json.loads(json_str)
                
                # 创建决策对象
                for item in data_list:
                    function_name = item.get("function_name")
                    # 查找匹配的函数
                    func = None
                    for f in functions:
                        if f.function_name == function_name or function_name in f.function_name:
                            func = f
                            break
                    
                    if func:
                        decisions.append(CandidateDecision(
                            need_modify=item.get("need_modify", False),
                            reason=item.get("reason", ""),
                            function=func,
                            start_line=item.get("start_line", func.start_line),
                            end_line=item.get("end_line", func.end_line),
                            original_code=item.get("original_code", func.code),
                            modified_hint=item.get("modified_hint"),
                            score=item.get("score", 0.0)
                        ))
                
                # 按分数排序
                decisions.sort(key=lambda x: x.score or 0.0, reverse=True)
                
        except Exception as e:
            logger.warning(f"Error parsing detailed locate response: {e}")
            # Fallback
            return self._create_fallback_decisions(functions, file_summary)
        
        return decisions if decisions else self._create_fallback_decisions(functions, file_summary)
    
    def _create_fallback_decisions(self,
                                  functions: List[FunctionInfo],
                                  file_summary: FileSummary) -> List[CandidateDecision]:
        """
        创建 fallback 决策（当 LLM 调用失败时）。
        
        Args:
            functions: 函数列表
            file_summary: 文件摘要
            
        Returns:
            CandidateDecision 列表
        """
        decisions = []
        for func in functions:
            decisions.append(CandidateDecision(
                need_modify=True,  # 默认都需要修改
                reason="Fallback: LLM filter unavailable",
                function=func,
                start_line=func.start_line,
                end_line=func.end_line,
                original_code=func.code,
                score=0.5  # 默认分数
            ))
        return decisions
    
    def _log_audit(self,
                  log_type: str,
                  problem_statement: str,
                  file_summary: FileSummary,
                  functions: List[FunctionInfo],
                  content: str):
        """
        记录审计日志。
        
        Args:
            log_type: 日志类型（"input" 或 "output"）
            problem_statement: 问题描述
            file_summary: 文件摘要
            functions: 函数列表
            content: 日志内容
        """
        if not self.audit_log_dir:
            return
        
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        log_file = self.audit_log_dir / f"llm_{log_type}_{timestamp}.json"
        
        log_data = {
            "timestamp": timestamp,
            "type": log_type,
            "mode": self.mode,
            "problem_statement": problem_statement,
            "file": file_summary.file_name,
            "functions_count": len(functions),
            "content": content
        }
        
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to write audit log: {e}")


