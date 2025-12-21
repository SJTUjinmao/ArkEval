"""
Candidate decision data structure for LLM filter output.
"""

from typing import Optional
from pydantic import BaseModel, Field

from .function_info import FunctionInfo


class CandidateDecision(BaseModel):
    """
    LLM 过滤器的决策结果。
    
    包含函数是否需要修改的判断、原因、精确位置、原始代码和可选的修改建议。
    """
    
    need_modify: bool = Field(..., description="Whether this function needs modification")
    reason: str = Field(..., description="Reason for the decision")
    function: FunctionInfo = Field(..., description="Function information")
    start_line: int = Field(..., description="Precise start line (may be more specific than function.start_line)")
    end_line: int = Field(..., description="Precise end line (may be more specific than function.end_line)")
    original_code: str = Field(..., description="Original code that needs modification")
    modified_hint: Optional[str] = Field(None, description="Optional hint for how to modify the function")
    score: Optional[float] = Field(None, description="Confidence score (0-1)")
    
    class Config:
        """Pydantic configuration."""
        pass

