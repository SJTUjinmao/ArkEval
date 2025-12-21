"""
File summary data structure for ArkTS files.
"""

from typing import List, Optional, Dict
from pydantic import BaseModel, Field


class FileSummary(BaseModel):
    """Structured summary of an ArkTS file."""
    
    file_name: str = Field(..., description="Name of the file")
    file_path: str = Field(..., description="Full path to the file")
    main_entry: Optional[str] = Field(None, description="Main entry point of the file")
    exports: List[str] = Field(default_factory=list, description="List of exported symbols")
    components: List[str] = Field(default_factory=list, description="List of components defined in the file")
    state_management: List[str] = Field(default_factory=list, description="State management patterns used")
    dependencies: List[str] = Field(default_factory=list, description="Dependencies and imports")
    summary: str = Field(..., description="Textual summary of the file's purpose")
    line_count: int = Field(..., description="Total number of lines in the file")
    file_hash: Optional[str] = Field(None, description="File hash (mtime + content hash) for incremental indexing")
    embedding: Optional[List[float]] = Field(None, description="Embedding vector for similarity search")
    
    class Config:
        """Pydantic configuration."""
        json_encoders = {
            list: lambda v: v,
        }

