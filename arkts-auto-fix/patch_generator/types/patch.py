"""
Patch data structures for patch_generator module.
"""

from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from pathlib import Path


class Patch(BaseModel):
    """补丁数据结构。"""
    
    patch_id: str = Field(..., description="Unique patch identifier")
    file_path: str = Field(..., description="File path to apply patch")
    old_code: str = Field(..., description="Original code")
    new_code: str = Field(..., description="Modified code")
    unified_diff: str = Field(..., description="Git-style unified diff")
    start_line: int = Field(..., description="Start line number")
    end_line: int = Field(..., description="End line number")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    
    class Config:
        """Pydantic configuration."""
        pass


class PatchRequest(BaseModel):
    """补丁生成请求。"""
    
    problem_statement: str = Field(..., description="Problem statement")
    locator_output_path: Path = Field(..., description="Path to locator output JSON")
    target_function_code: str = Field(..., description="Target function code to modify")
    file_path: str = Field(..., description="File path containing the function")
    start_line: int = Field(0, description="Start line number of the function")
    end_line: int = Field(0, description="End line number of the function")
    context: Optional[str] = Field(None, description="Additional context")
    
    class Config:
        """Pydantic configuration."""
        pass


class PatchResult(BaseModel):
    """补丁生成结果。"""
    
    patch: Patch = Field(..., description="Generated patch")
    applied: bool = Field(False, description="Whether patch has been applied")
    llm_scores: Optional[Dict[str, float]] = Field(None, description="LLM confidence scores")
    error: Optional[str] = Field(None, description="Error message if generation failed")
    tests_to_run: Optional[List[str]] = Field(None, description="Suggested tests to run")
    
    class Config:
        """Pydantic configuration."""
        pass

