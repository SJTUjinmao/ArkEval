"""
函数提取器 - 从 ArkTS 文件中提取函数级信息。

根据模块解析要求：
- 使用 Python 正则 + 简单括号计数来稳健提取大多数函数/方法
- 对 class/struct 的方法也要支持
- 提供 fragment 切分支持（3-12 行/语义边界）
- 标注 ast_parse_failed 字段以便 fallback
"""

from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
import re
import logging

from .types.function_info import FunctionInfo

logger = logging.getLogger(__name__)


class FunctionExtractor:
    """
    函数提取器 - 从 ArkTS 文件中提取函数信息。
    
    支持：
        - 普通函数提取
        - class/struct 内的方法提取
        - 函数片段（fragment）切分
        - 字节位置计算
    """
    
    def __init__(self):
        """初始化函数提取器。"""
        # 普通函数模式
        self.function_pattern = re.compile(
            r'(?:public\s+|private\s+|protected\s+|internal\s+)?'
            r'(?:static\s+)?'
            r'(?:async\s+)?'
            r'function\s+(\w+)\s*\([^)]*\)\s*(?::\s*[\w\[\]<>|&,.\s]+)?\s*\{',
            re.MULTILINE
        )
        
        # 类方法模式（在 class/struct 内部）
        self.method_pattern = re.compile(
            r'(?:public\s+|private\s+|protected\s+)?'
            r'(?:static\s+)?'
            r'(?:async\s+)?'
            r'(\w+)\s*\([^)]*\)\s*(?::\s*[\w\[\]<>|&,.\s]+)?\s*\{',
            re.MULTILINE
        )
        
        # 装饰器模式
        self.decorator_pattern = re.compile(r'@\w+', re.MULTILINE)
    
    def extract(self, 
                file_path: Path, 
                content: Optional[str] = None,
                fragment: bool = False,
                fragment_size: int = 8) -> List[FunctionInfo]:
        """
        从 ArkTS 文件中提取函数信息。
        
        Args:
            file_path: ArkTS 文件路径
            content: 文件内容（如果提供则不再读取文件）
            fragment: 是否将函数切分为片段
            fragment_size: 每个片段的行数（3-12 行）
            
        Returns:
            List[FunctionInfo]: 函数信息列表
        """
        try:
            if content is None:
                content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            raise
        
        functions = []
        lines = content.splitlines()
        
        # 提取普通函数
        functions.extend(self._extract_functions(content, lines, file_path, fragment, fragment_size))
        
        # 提取类/结构体中的方法
        functions.extend(self._extract_class_methods(content, lines, file_path, fragment, fragment_size))
        
        logger.info(f"Extracted {len(functions)} functions from {file_path.name}")
        
        return functions
    
    def _extract_functions(self, 
                          content: str, 
                          lines: List[str],
                          file_path: Path,
                          fragment: bool,
                          fragment_size: int) -> List[FunctionInfo]:
        """提取普通函数。"""
        functions = []
        ast_parse_failed = False
        
        try:
            for match in self.function_pattern.finditer(content):
                function_name = match.group(1)
                start_pos = match.start()
                start_line = content[:start_pos].count('\n') + 1
                start_byte = start_pos
                
                # 查找函数结束位置
                end_pos, end_line = self._find_function_end(content, start_pos, lines)
                end_byte = end_pos
                
                # 提取函数代码
                function_code = '\n'.join(lines[start_line - 1:end_line])
                
                # 提取签名和装饰器
                signature, decorators, is_async, access_modifier = self._extract_function_metadata(
                    content, start_line, lines
                )
                
                # 提取参数和返回类型
                parameters, return_type = self._extract_signature_info(signature)
                
                # 切分片段（如果需要）
                fragments = None
                if fragment and (end_line - start_line) > fragment_size:
                    fragments = self._split_into_fragments(
                        function_code, start_line, fragment_size
                    )
                
                function_info = FunctionInfo(
                    function_name=function_name,
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    code=function_code,
                    file_path=str(file_path),
                    fragments=fragments,
                    signature=signature,
                    parameters=parameters,
                    return_type=return_type,
                    decorators=decorators,
                    is_async=is_async,
                    access_modifier=access_modifier,
                    ast_parse_failed=ast_parse_failed
                )
                
                functions.append(function_info)
        except Exception as e:
            logger.warning(f"Error extracting functions (falling back to simple extraction): {e}")
            ast_parse_failed = True
        
        return functions
    
    def _extract_class_methods(self,
                              content: str,
                              lines: List[str],
                              file_path: Path,
                              fragment: bool,
                              fragment_size: int) -> List[FunctionInfo]:
        """提取类/结构体中的方法。"""
        functions = []
        
        # 查找所有 class 和 struct
        class_pattern = re.compile(r'(?:export\s+)?(?:class|struct)\s+(\w+)', re.MULTILINE)
        
        for class_match in class_pattern.finditer(content):
            class_start = class_match.end()
            class_name = class_match.group(1)
            
            # 找到类的结束位置
            class_end = self._find_class_end(content, class_start)
            class_content = content[class_start:class_end]
            
            # 在类内容中查找方法
            for method_match in self.method_pattern.finditer(class_content):
                method_name = method_match.group(1)
                # 跳过构造函数和特殊方法名
                if method_name in ['constructor', 'build', 'aboutToAppear', 'aboutToDisappear']:
                    continue
                
                method_start_in_class = method_match.start()
                method_start_global = class_start + method_start_in_class
                start_line = content[:method_start_global].count('\n') + 1
                start_byte = method_start_global
                
                # 查找方法结束位置
                end_pos, end_line = self._find_function_end(content, method_start_global, lines)
                end_byte = end_pos
                
                # 提取方法代码
                method_code = '\n'.join(lines[start_line - 1:end_line])
                
                # 提取元数据
                signature, decorators, is_async, access_modifier = self._extract_function_metadata(
                    content, start_line, lines
                )
                parameters, return_type = self._extract_signature_info(signature)
                
                # 切分片段
                fragments = None
                if fragment and (end_line - start_line) > fragment_size:
                    fragments = self._split_into_fragments(
                        method_code, start_line, fragment_size
                    )
                
                function_info = FunctionInfo(
                    function_name=f"{class_name}.{method_name}",
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=start_byte,
                    end_byte=end_byte,
                    code=method_code,
                    file_path=str(file_path),
                    fragments=fragments,
                    signature=signature,
                    parameters=parameters,
                    return_type=return_type,
                    decorators=decorators,
                    is_async=is_async,
                    access_modifier=access_modifier,
                    ast_parse_failed=False
                )
                
                functions.append(function_info)
        
        return functions
    
    def _find_function_end(self, content: str, start_pos: int, lines: List[str]) -> Tuple[int, int]:
        """
        查找函数结束位置（通过匹配大括号）。
        
        Returns:
            (end_byte_position, end_line_number)
        """
        brace_count = 0
        in_function = False
        pos = start_pos
        in_string = False
        string_char = None
        
        while pos < len(content):
            char = content[pos]
            
            # 处理字符串字面量
            if char in ['"', "'", '`'] and (pos == 0 or content[pos-1] != '\\'):
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    in_string = False
                    string_char = None
            
            if not in_string:
                if char == '{':
                    brace_count += 1
                    in_function = True
                elif char == '}':
                    brace_count -= 1
                    if in_function and brace_count == 0:
                        end_line = content[:pos + 1].count('\n') + 1
                        return pos + 1, end_line
            
            pos += 1
        
        # 如果找不到结束位置，返回文件末尾
        return len(content), len(lines)
    
    def _find_class_end(self, content: str, start_pos: int) -> int:
        """查找类/结构体的结束位置。"""
        brace_count = 0
        pos = start_pos
        
        while pos < len(content):
            char = content[pos]
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    return pos + 1
            pos += 1
        
        return len(content)
    
    def _extract_function_metadata(self, 
                                   content: str, 
                                   start_line: int, 
                                   lines: List[str]) -> Tuple[Optional[str], Optional[List[str]], Optional[bool], Optional[str]]:
        """提取函数元数据（签名、装饰器、是否异步、访问修饰符）。"""
        if start_line > len(lines):
            return None, None, None, None
        
        # 向前查找函数声明（最多向前看 5 行）
        func_lines = []
        for i in range(max(0, start_line - 5), start_line):
            func_lines.append(lines[i])
        
        full_declaration = '\n'.join(func_lines)
        
        # 提取装饰器
        decorators = self.decorator_pattern.findall(full_declaration)
        decorators = decorators if decorators else None
        
        # 检查是否异步
        is_async = 'async' in full_declaration
        
        # 提取访问修饰符
        access_modifier = None
        for modifier in ['public', 'private', 'protected', 'internal']:
            if modifier in full_declaration:
                access_modifier = modifier
                break
        
        # 提取签名（函数声明行）
        signature = lines[start_line - 1].strip() if start_line <= len(lines) else None
        
        return signature, decorators, is_async, access_modifier
    
    def _extract_signature_info(self, signature: Optional[str]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """从签名中提取参数和返回类型。"""
        if not signature:
            return None, None
        
        parameters = []
        return_type = None
        
        # 简单的参数提取（可以改进）
        param_match = re.search(r'\(([^)]*)\)', signature)
        if param_match:
            param_str = param_match.group(1)
            if param_str.strip():
                for param in param_str.split(','):
                    param = param.strip()
                    if param:
                        # 简单的参数解析
                        parts = param.split(':')
                        param_name = parts[0].strip()
                        param_type = parts[1].strip() if len(parts) > 1 else None
                        parameters.append({
                            'name': param_name,
                            'type': param_type,
                            'optional': '?' in param_name
                        })
        
        # 提取返回类型
        return_match = re.search(r':\s*([\w\[\]<>|&,.\s]+)', signature)
        if return_match:
            return_type = return_match.group(1).strip()
        
        return parameters if parameters else None, return_type
    
    def _split_into_fragments(self, 
                             code: str, 
                             start_line: int, 
                             fragment_size: int) -> List[Dict[str, Any]]:
        """
        将函数代码切分为多个片段（3-12 行/语义边界）。
        
        Args:
            code: 函数代码
            start_line: 函数起始行号
            fragment_size: 每个片段的行数
            
        Returns:
            片段列表，每个片段包含 start_line, end_line, code
        """
        lines = code.splitlines()
        fragments = []
        
        for i in range(0, len(lines), fragment_size):
            fragment_lines = lines[i:i + fragment_size]
            fragment_code = '\n'.join(fragment_lines)
            
            fragment_start = start_line + i
            fragment_end = start_line + i + len(fragment_lines) - 1
            
            fragments.append({
                'start_line': fragment_start,
                'end_line': fragment_end,
                'code': fragment_code,
                'line_count': len(fragment_lines)
            })
        
        return fragments

