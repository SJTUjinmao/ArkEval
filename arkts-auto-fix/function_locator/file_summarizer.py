"""
File summarizer for generating structured summaries of ArkTS files.
"""

from pathlib import Path
from typing import Optional
import logging
import json
import sys
from pathlib import Path as PathLib

# 添加项目根目录到路径
project_root = PathLib(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from pangu_model import PanguModel

from .types.file_summary import FileSummary
from .config import Config

logger = logging.getLogger(__name__)


class FileSummarizer:
    """Generates structured summaries for ArkTS files."""
    
    def __init__(self, use_cache: bool = True, pangu_model_path: str = None):
        """
        Initialize file summarizer.
        
        Args:
            use_cache: Whether to use cached summaries
            pangu_model_path: Pangu 模型路径
        """
        self.use_cache = use_cache and Config.CACHE_SUMMARIES
        self.cache_dir = Config.get_summaries_cache_dir()
        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化 Pangu 模型
        self.pangu_model_path = pangu_model_path or getattr(Config, 'PANGU_MODEL_PATH', '/opt/pangu/openPangu-Embedded-7B-V1.1')
        try:
            self.pangu_model = PanguModel(model_path=self.pangu_model_path)
        except Exception as e:
            logger.error(f"初始化 Pangu 模型失败: {e}")
            raise
    
    def summarize(self, file_path: Path, content: Optional[str] = None) -> FileSummary:
        """
        Generate a structured summary for an ArkTS file.
        
        Args:
            file_path: Path to the ArkTS file
            content: Optional file content (if provided, will not read from file)
            
        Returns:
            FileSummary object
        """
        # Check cache first
        if self.use_cache:
            cached = self._load_from_cache(file_path)
            if cached:
                return cached
        
        # Read file content if not provided
        if content is None:
            try:
                content = file_path.read_text(encoding='utf-8')
            except Exception as e:
                logger.error(f"Error reading file {file_path}: {e}")
                raise
        
        # Count lines
        line_count = len(content.splitlines())
        
        # Parse file to extract structured information
        # This is a simplified version - you may want to use AST parser here
        summary_data = self._parse_file(content, file_path)
        
        file_summary = FileSummary(
            file_name=file_path.name,
            file_path=str(file_path),
            main_entry=summary_data.get('main_entry'),
            exports=summary_data.get('exports', []),
            components=summary_data.get('components', []),
            state_management=summary_data.get('state_management', []),
            dependencies=summary_data.get('dependencies', []),
            summary=summary_data.get('summary', ''),
            line_count=line_count
        )
        
        # Save to cache
        if self.use_cache:
            self._save_to_cache(file_path, file_summary)
        
        return file_summary
    
    def _parse_file(self, content: str, file_path: Path) -> dict:
        """
        Parse file content to extract structured information.
        
        Args:
            content: File content as string
            file_path: Path to the file
            
        Returns:
            Dictionary with parsed information
        """
        lines = content.splitlines()
        exports = []
        components = []
        dependencies = []
        state_management = []
        
        # Extract structured information
        for line in lines:
            # Extract imports/dependencies
            if 'import' in line and line.strip().startswith('import'):
                dependencies.append(line.strip())
            
            # Extract components
            if '@Component' in line:
                # Try to extract component name from next line
                idx = lines.index(line)
                if idx + 1 < len(lines):
                    next_line = lines[idx + 1]
                    if 'export struct' in next_line or 'export default struct' in next_line:
                        # Extract struct name
                        parts = next_line.split()
                        for i, part in enumerate(parts):
                            if part == 'struct' and i + 1 < len(parts):
                                components.append(parts[i + 1])
                                break
            
            # Extract state management
            if '@State' in line or '@Link' in line or '@Consume' in line or '@Provide' in line:
                state_management.append(line.strip())
        
        # Generate meaningful summary using LLM
        summary = self._generate_summary_with_llm(content, file_path, components, dependencies)
        
        return {
            'main_entry': None,
            'exports': exports,
            'components': components,
            'state_management': state_management,
            'dependencies': dependencies,
            'summary': summary
        }
    
    def _generate_summary_with_llm(self, content: str, file_path: Path, 
                                   components: list, dependencies: list) -> str:
        """
        Generate a meaningful summary of the file using LLM.
        
        Args:
            content: File content
            file_path: Path to the file
            components: List of component names found
            dependencies: List of dependencies
            
        Returns:
            Summary string describing the file's purpose and functionality
        """
        try:
            # Limit content length to avoid exceeding context window
            # Take first 2000 characters and last 500 characters for context
            max_content_length = 2000
            if len(content) > max_content_length:
                preview_content = content[:max_content_length] + "\n... (file truncated) ...\n" + content[-500:]
            else:
                preview_content = content
            
            # Build prompt for LLM
            prompt = f"""请分析以下 ArkTS 文件，生成一个简洁的文件摘要，描述该文件的主要功能和用途。

文件路径: {file_path.name}
组件: {', '.join(components) if components else '无'}
依赖数量: {len(dependencies)}

文件内容:
```
{preview_content}
```

请用中文生成一个简洁的摘要（1-3句话），描述：
1. 该文件的主要功能
2. 包含的主要组件或类
3. 在应用中的作用

只返回摘要文本，不要包含其他说明。"""

            # Call LLM via Pangu model with retry
            max_retries = 3
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        logger.info(f"Retrying LLM summary request (attempt {attempt + 1}/{max_retries})...")
                        import time
                        time.sleep(2)
                    
                    summary = self.pangu_model.generate(
                        prompt=prompt,
                        max_new_tokens=200,
                        temperature=0.3,
                        max_length=min(Config.LLM_CONTEXT_SIZE, 4096)
                    ).strip()
                    
                    if summary:
                        break
                    else:
                        raise ValueError("Empty response from LLM")
                        
                except Exception as e:
                    last_error = str(e)
                    if attempt == max_retries - 1:
                        raise
                    logger.warning(f"LLM summary request failed (attempt {attempt + 1}): {e}")
                    continue
            
            # 如果所有重试都失败，检查是否成功获取了 summary
            if 'summary' not in locals() or not summary:
                raise Exception(f"Failed to generate summary after {max_retries} attempts. Last error: {last_error}")
            
            # Clean up the summary (remove any extra formatting)
            if summary:
                # Remove common LLM prefixes/suffixes
                summary = summary.replace('摘要：', '').replace('摘要:', '').strip()
                # Take first sentence or first 200 characters
                if len(summary) > 200:
                    summary = summary[:200].rsplit('。', 1)[0] + '。'
                return summary
            else:
                # Fallback to basic summary
                return self._generate_fallback_summary(file_path, components, dependencies)
                
        except Exception as e:
            logger.warning(f"Failed to generate LLM summary for {file_path}: {e}, using fallback")
            return self._generate_fallback_summary(file_path, components, dependencies)
    
    def _generate_fallback_summary(self, file_path: Path, components: list, dependencies: list) -> str:
        """
        Generate a fallback summary when LLM is unavailable.
        
        Args:
            file_path: Path to the file
            components: List of component names
            dependencies: List of dependencies
            
        Returns:
            Basic summary string
        """
        file_name = file_path.stem
        file_path_str = str(file_path)
        
        # Try to infer purpose from file name, path, and components
        summary_parts = []
        
        # Infer purpose from file path
        if 'pages' in file_path_str:
            if 'Login' in file_path_str:
                if 'Password' in file_name:
                    summary_parts.append("密码输入页面")
                elif 'VerifyCode' in file_name:
                    summary_parts.append("验证码输入页面")
                elif 'PhoneNumber' in file_name:
                    summary_parts.append("手机号输入页面")
                else:
                    summary_parts.append("登录流程页面")
            elif 'Chat' in file_path_str:
                summary_parts.append("聊天相关页面")
            else:
                summary_parts.append("页面组件")
        elif 'views' in file_path_str:
            if 'Chat' in file_path_str:
                if 'Detail' in file_name:
                    if 'Bottom' in file_name:
                        summary_parts.append("聊天详情页面底部输入栏组件")
                    elif 'Top' in file_name:
                        summary_parts.append("聊天详情页面顶部组件")
                    else:
                        summary_parts.append("聊天详情页面组件")
                elif 'List' in file_name:
                    summary_parts.append("聊天列表组件")
                else:
                    summary_parts.append("聊天界面组件")
            else:
                summary_parts.append("视图组件")
        elif 'viewmodel' in file_path_str or 'ViewModel' in file_name:
            summary_parts.append("视图模型，负责数据管理和状态管理")
        elif 'DataSource' in file_name:
            summary_parts.append("数据源，管理数据加载和更新")
        elif 'entities' in file_path_str or 'Entity' in file_name:
            summary_parts.append("数据实体类，定义数据结构")
        elif 'dao' in file_path_str or 'Dao' in file_name:
            summary_parts.append("数据访问对象，负责数据库操作")
        elif 'constants' in file_path_str:
            summary_parts.append("常量定义文件")
        elif 'utils' in file_path_str or 'Utils' in file_name or 'Helper' in file_name:
            summary_parts.append("工具函数和辅助方法")
        else:
            # Infer from file name
            if 'Chat' in file_name:
                summary_parts.append("聊天相关功能")
            elif 'Message' in file_name:
                summary_parts.append("消息相关功能")
            elif 'Login' in file_name or 'Auth' in file_name:
                summary_parts.append("登录认证功能")
            else:
                summary_parts.append("ArkTS 组件文件")
        
        # Add component information if available
        if components:
            if len(components) == 1:
                summary_parts.append(f"主要组件: {components[0]}")
            else:
                summary_parts.append(f"包含 {len(components)} 个组件")
        
        if summary_parts:
            return "，".join(summary_parts)
        else:
            return f"ArkTS 文件: {file_name}"
    
    def _load_from_cache(self, file_path: Path) -> Optional[FileSummary]:
        """Load summary from cache if available."""
        cache_file = self.cache_dir / f"{file_path.stem}.json"
        if cache_file.exists():
            try:
                import json
                data = json.loads(cache_file.read_text())
                return FileSummary(**data)
            except Exception as e:
                logger.warning(f"Error loading cache for {file_path}: {e}")
        return None
    
    def _save_to_cache(self, file_path: Path, summary: FileSummary):
        """Save summary to cache."""
        cache_file = self.cache_dir / f"{file_path.stem}.json"
        try:
            import json
            cache_file.write_text(json.dumps(summary.dict(), indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"Error saving cache for {file_path}: {e}")


