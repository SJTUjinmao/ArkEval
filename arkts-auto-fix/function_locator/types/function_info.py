"""
Function information data structure.
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class FunctionInfo(BaseModel):
    """
    Information about a function in an ArkTS file.
    
    用于存储从 ArkTS 文件中提取的函数信息，包括位置、代码、签名等。
    支持函数片段（fragments）以便进行更细粒度的分析。
    """
    
    # 基本信息
    function_name: str = Field(..., description="Name of the function")
    file_path: str = Field(..., description="Path to the file containing this function")
    
    # 位置信息
    start_line: int = Field(..., description="Starting line number (1-indexed)")
    end_line: int = Field(..., description="Ending line number (1-indexed)")
    start_byte: Optional[int] = Field(None, description="Starting byte position in the file")
    end_byte: Optional[int] = Field(None, description="Ending byte position in the file")
    
    # 代码内容
    code: str = Field(..., description="Full function code")
    fragments: Optional[List[Dict[str, Any]]] = Field(None, description="Function fragments (if function is split into fragments, 3-12 lines per fragment)")
    
    # 函数签名信息
    signature: Optional[str] = Field(None, description="Function signature (e.g., 'public async functionName(param: Type): ReturnType')")
    parameters: Optional[List[Dict[str, Any]]] = Field(None, description="Function parameters list, each with name, type, optional, etc.")
    return_type: Optional[str] = Field(None, description="Return type of the function")
    
    # ArkTS 特定信息（可选）
    decorators: Optional[List[str]] = Field(None, description="Decorators applied to the function (e.g., '@Builder', '@Extend', '@Concurrent')")
    is_async: Optional[bool] = Field(None, description="Whether the function is async")
    access_modifier: Optional[str] = Field(None, description="Access modifier (public, private, protected, internal)")
    is_lifecycle: Optional[bool] = Field(None, description="Whether this is a lifecycle method (onPageShow, onPageHide, etc.)")
    
    # 解析状态
    ast_parse_failed: Optional[bool] = Field(False, description="Whether AST parsing failed (for fallback handling)")
    
    class Config:
        """Pydantic configuration."""
        pass

