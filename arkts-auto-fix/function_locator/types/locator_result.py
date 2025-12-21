"""
Locator result data structure - final output of function_locator.
"""

from typing import List, Optional
from pydantic import BaseModel, Field

from .function_info import FunctionInfo


class Candidate(BaseModel):
    """A candidate function that might need modification."""
    
    file: str = Field(..., description="File path containing the candidate function")
    function: FunctionInfo = Field(..., description="Function information")
    reason: str = Field(..., description="Reason why this function is a candidate")
    score: float = Field(..., description="Similarity score or confidence score")
    original_code: str = Field(..., description="Original code of the function")
    modified_hint: Optional[str] = Field(None, description="Optional hint for how to modify the function")


class LocatorResult(BaseModel):
    """Final result from function locator."""
    
    problem_statement: str = Field(..., description="The problem statement that was analyzed")
    candidates: List[Candidate] = Field(default_factory=list, description="List of candidate functions that might need modification")
    
    # 向后兼容字段（可选，用于简化访问）
    target_function: Optional[FunctionInfo] = Field(None, description="The function most likely to need modification (first candidate)")
    reasoning: Optional[str] = Field(None, description="Reasoning for why the target function was selected")
    code_before: Optional[str] = Field(None, description="Code before modification (the original function code)")
    candidate_functions: Optional[List[FunctionInfo]] = Field(None, description="All candidate functions considered (deprecated, use candidates)")
    matched_files: Optional[List[str]] = Field(None, description="Files that matched the similarity search")
    
    class Config:
        """Pydantic configuration."""
        pass

