"""
LLM 补丁生成 - 封装 LLM 调用生成代码修改建议。

根据模块解析要求：
- 封装 LLM 调用（qwen3-coder-30b 或其他模型）生成代码修改建议
- 支持多轮（若第一次失败可带上错误信息再次询问）
- 强制 LLM 输出可解析的 JSON schema
- 限制 token，防止输出整文件（只请求函数体）
"""

from typing import Optional, Dict, Any
import logging
import json
import re
import sys
from pathlib import Path as PathLib

# 添加项目根目录到路径
project_root = PathLib(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from pangu_model import PanguModel

from .types.patch import Patch, PatchRequest, PatchResult

logger = logging.getLogger(__name__)


class LLMPatch:
    """
    LLM 补丁生成器 - 使用 LLM 生成代码修改建议。
    
    功能：
        - 调用 LLM 生成新代码
        - 支持多轮对话（失败重试）
        - 强制 JSON 输出格式
        - 限制输出长度
    """
    
    def __init__(self,
                 pangu_model_path: str = "/opt/pangu/openPangu-Embedded-7B-V1.1",
                 max_retries: int = 3,
                 context_size: int = 16384):
        """
        初始化 LLM 补丁生成器。
        
        Args:
            pangu_model_path: Pangu 模型路径
            max_retries: 最大重试次数
            context_size: 上下文窗口大小（num_ctx，默认 16384，即 16k）
        """
        self.pangu_model_path = pangu_model_path
        self.max_retries = max_retries
        self.context_size = context_size
        
        # 初始化 Pangu 模型
        try:
            self.pangu_model = PanguModel(model_path=self.pangu_model_path)
        except Exception as e:
            logger.error(f"初始化 Pangu 模型失败: {e}")
            raise
    
    def generate_patch(self, request: PatchRequest) -> PatchResult:
        """
        生成补丁。
        
        Args:
            request: 补丁生成请求
            
        Returns:
            PatchResult: 补丁生成结果
        """
        prompt = self._build_prompt(request)
        
        for attempt in range(self.max_retries):
            try:
                logger.info(f"Generating patch (attempt {attempt + 1}/{self.max_retries})...")
                
                response = self._call_llm(prompt)
                result = self._parse_response(response, request)
                
                if result.patch:
                    return result
                else:
                    # 如果解析失败，在 prompt 中添加错误信息重试
                    if attempt < self.max_retries - 1:
                        prompt = self._add_error_context(prompt, result.error)
                        logger.warning(f"Retrying with error context: {result.error}")
                    
            except Exception as e:
                logger.error(f"Error generating patch (attempt {attempt + 1}): {e}")
                if attempt == self.max_retries - 1:
                    return PatchResult(
                        patch=Patch(
                            patch_id="",
                            file_path=request.file_path,
                            old_code=request.target_function_code,
                            new_code="",
                            unified_diff="",
                            start_line=0,
                            end_line=0
                        ),
                        error=str(e)
                    )
        
        return PatchResult(
            patch=Patch(
                patch_id="",
                file_path=request.file_path,
                old_code=request.target_function_code,
                new_code="",
                unified_diff="",
                start_line=0,
                end_line=0
            ),
            error="Failed to generate patch after all retries"
        )
    
    def _build_prompt(self, request: PatchRequest) -> str:
        """
        构建 LLM prompt。
        
        Args:
            request: 补丁生成请求
            
        Returns:
            Formatted prompt string
        """
        prompt_parts = [
            "You are a code modification assistant for ArkTS.",
            "",
            "Problem Statement:",
            request.problem_statement,
            "",
            "File:", request.file_path,
            "",
            "Original Function Code:",
            "```arkts",
            request.target_function_code,
            "```",
            "",
            "Requirements:",
            "1. Keep the function interface unchanged (signature, parameters, return type)",
            "2. Make minimal changes to solve the problem",
            "3. Only modify the function body, not the entire file",
            "4. Provide tests_to_run if applicable",
            "",
            "Respond with JSON in this format:",
            "{",
            '  "new_code": "<modified function code>",',
            '  "explanation": "<brief explanation of changes>",',
            '  "tests_to_run": ["test1", "test2"]',
            "}",
        ]
        
        if request.context:
            prompt_parts.extend([
                "",
                "Additional Context:",
                request.context[:500]  # 限制上下文长度
            ])
        
        return "\n".join(prompt_parts)
    
    def _call_llm(self, prompt: str) -> str:
        """
        调用 LLM。
        
        Args:
            prompt: Prompt 文本
            
        Returns:
            LLM 响应文本
        """
        try:
            response = self.pangu_model.generate(
                prompt=prompt,
                max_new_tokens=2048,
                temperature=0.1,
                max_length=self.context_size
            )
            return response
        except Exception as e:
            logger.error(f"Pangu 模型调用失败: {e}")
            raise
    
    def _parse_response(self, response: str, request: PatchRequest) -> PatchResult:
        """
        解析 LLM 响应。
        
        Args:
            response: LLM 响应文本
            request: 补丁生成请求
            
        Returns:
            PatchResult: 解析结果
        """
        try:
            # 提取 JSON
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            
            if json_start < 0 or json_end <= json_start:
                return PatchResult(
                    patch=Patch(
                        patch_id="",
                        file_path=request.file_path,
                        old_code=request.target_function_code,
                        new_code="",
                        unified_diff="",
                        start_line=0,
                        end_line=0
                    ),
                    error="No JSON found in response"
                )
            
            json_str = response[json_start:json_end]
            
            # 清理 JSON 字符串：移除控制字符和无效字符
            import re
            # 移除控制字符（除了换行符、制表符等）
            json_str = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', json_str)
            
            # 修复反引号问题：将反引号字符串转换为双引号字符串
            # LLM 可能使用反引号包裹代码字符串，需要转换为有效的 JSON 字符串
            def replace_backtick_string(match):
                content = match.group(1)
                # 先转义反斜杠（必须在其他转义之前）
                content = content.replace('\\', '\\\\')
                # 转义双引号
                content = content.replace('"', '\\"')
                # 转义换行符、回车符、制表符
                content = content.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
                return f'"{content}"'
            
            # 匹配反引号字符串（使用 DOTALL 以匹配多行）
            # 注意：需要处理嵌套的情况，但这里假设代码中不会出现反引号
            json_str = re.sub(r'`([^`]*)`', replace_backtick_string, json_str, flags=re.DOTALL)
            
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError as e:
                # 如果还是失败，尝试更宽松的解析
                # 移除可能的注释
                json_str_clean = re.sub(r'//.*?$', '', json_str, flags=re.MULTILINE)
                json_str_clean = re.sub(r'/\*.*?\*/', '', json_str_clean, flags=re.DOTALL)
                # 再次尝试
                try:
                    data = json.loads(json_str_clean)
                except json.JSONDecodeError as e2:
                    # 最后尝试：手动提取 new_code（如果 JSON 格式严重损坏）
                    # 1) 尝试从反引号中提取代码（旧兼容逻辑）
                    backtick_match = re.search(r'"new_code"\s*:\s*`([^`]*)`', response, re.DOTALL)
                    if backtick_match:
                        new_code = backtick_match.group(1)
                        data = {
                            "new_code": new_code,
                            "explanation": "Extracted from backtick string (JSON parsing failed)",
                            "tests_to_run": []
                        }
                    else:
                        # 2) 兼容 LLM 输出中使用多行双引号字符串的情况：
                        #    例如 "new_code": "\n  line1\n  line2\n"
                        #    这种写法在严格 JSON 里是不允许的（需要转义换行），
                        #    这里直接用正则把整段内容摘出来当作代码。
                        dq_match = re.search(
                            r'"new_code"\s*:\s*"(.*?)"\s*,\s*"\s*explanation"',
                            json_str_clean or json_str,
                            re.DOTALL
                        )
                        if dq_match:
                            new_code = dq_match.group(1)
                            # 还原被转义的双引号
                            new_code = new_code.replace('\\"', '"')
                            data = {
                                "new_code": new_code,
                                "explanation": "Extracted from double-quoted string (JSON parsing failed)",
                                "tests_to_run": []
                            }
                        else:
                            # 保存原始响应用于调试
                            import os
                            from pathlib import Path
                            debug_dir = Path("test_output/debug_responses")
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            file_name = Path(request.file_path).stem
                            debug_file = debug_dir / f"{file_name}_json_error.txt"
                            try:
                                debug_file.write_text(
                                    f"Original Response:\n{response}\n\n"
                                    f"Extracted JSON:\n{json_str[:1000]}\n\n"
                                    f"Error: {e2}\n",
                                    encoding='utf-8'
                                )
                                logger.warning(f"Saved JSON error details to {debug_file}")
                            except:
                                pass
                            raise e2
            
            new_code = data.get("new_code", "")
            if not new_code:
                return PatchResult(
                    patch=Patch(
                        patch_id="",
                        file_path=request.file_path,
                        old_code=request.target_function_code,
                        new_code="",
                        unified_diff="",
                        start_line=0,
                        end_line=0
                    ),
                    error="No new_code in response"
                )
            
            # 提取函数体（去除函数签名，只保留函数体）
            new_code = self._extract_function_body(new_code)
            
            # 使用 locator 提供的准确行号
            start_line = request.start_line if request.start_line > 0 else 1
            end_line = request.end_line if request.end_line > 0 else len(request.target_function_code.splitlines())
            
            patch = Patch(
                patch_id="",
                file_path=request.file_path,
                old_code=request.target_function_code,
                new_code=new_code,
                unified_diff="",  # 由 formatter 填充
                start_line=start_line,
                end_line=end_line,
                metadata={
                    "explanation": data.get("explanation", ""),
                    "llm_model": self.pangu_model_path
                }
            )
            
            return PatchResult(
                patch=patch,
                llm_scores={"confidence": 0.8},  # 可以改进为实际评分
                tests_to_run=data.get("tests_to_run")
            )
            
        except json.JSONDecodeError as e:
            # 保存原始响应用于调试
            import os
            from pathlib import Path
            debug_dir = Path("test_output/debug_responses")
            debug_dir.mkdir(parents=True, exist_ok=True)
            
            file_name = Path(request.file_path).stem
            debug_file = debug_dir / f"{file_name}_raw_response.txt"
            try:
                debug_file.write_text(f"Original Response:\n{response}\n\nError: {e}\n\nExtracted JSON (first 500 chars):\n{json_str[:500] if 'json_str' in locals() else 'N/A'}", encoding='utf-8')
                logger.warning(f"Saved raw response to {debug_file} for debugging")
            except Exception as debug_err:
                logger.warning(f"Failed to save debug response: {debug_err}")
            
            return PatchResult(
                patch=Patch(
                    patch_id="",
                    file_path=request.file_path,
                    old_code=request.target_function_code,
                    new_code="",
                    unified_diff="",
                    start_line=0,
                    end_line=0
                ),
                error=f"JSON decode error: {e}"
            )
        except Exception as e:
            return PatchResult(
                patch=Patch(
                    patch_id="",
                    file_path=request.file_path,
                    old_code=request.target_function_code,
                    new_code="",
                    unified_diff="",
                    start_line=0,
                    end_line=0
                ),
                error=str(e)
            )
    
    def _extract_function_body(self, code: str) -> str:
        """
        提取函数体（去除函数签名）。
        
        Args:
            code: 完整函数代码
            
        Returns:
            函数体代码
        """
        lines = code.splitlines()
        # 查找第一个 { 所在行
        for i, line in enumerate(lines):
            if '{' in line:
                # 返回从 { 之后的内容
                return '\n'.join(lines[i:])
        return code
    
    def _add_error_context(self, prompt: str, error: Optional[str]) -> str:
        """
        在 prompt 中添加错误上下文（用于重试）。
        
        Args:
            prompt: 原始 prompt
            error: 错误信息
            
        Returns:
            更新后的 prompt
        """
        return prompt + f"\n\nPrevious error: {error}\nPlease fix the issue and try again."

