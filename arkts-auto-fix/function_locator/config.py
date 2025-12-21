"""
Configuration settings for function_locator.

支持从 YAML 文件读取配置，并支持环境变量覆盖。
配置优先级：环境变量 > YAML 文件 > 默认值
"""

from pathlib import Path
from typing import Optional, List
import os
import logging

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    logging.warning("PyYAML not installed, YAML config files will be ignored")

logger = logging.getLogger(__name__)


class Config:
    """Configuration class for function locator.
    
    支持从 YAML 文件和环境变量读取配置。
    配置优先级：环境变量 > YAML 文件 > 默认值
    """
    
    # ==================== Pangu 模型配置 ====================
    PANGU_MODEL_PATH: str = "/opt/pangu/openPangu-Embedded-7B-V1.1"  # Pangu 模型路径
    
    # ==================== Ollama 模型配置（已废弃，保留用于兼容性）====================
    OLLAMA_HOST: str = "http://localhost:11500"  # Ollama API 服务地址
    OLLAMA_EMBEDDING_MODEL: str = "qwen3-embedding:8b"  # 嵌入模型名称
    # 默认使用更强大的代码模型 qwen3-coder:30b（需要更高显存，但定位和理解能力更好）
    OLLAMA_LLM_MODEL: str = "qwen3-coder:30b"
    
    # ==================== 相似度搜索配置 ====================
    TOP_K_FILES: int = 10  # 相似度搜索返回的 top-k 文件数量
    
    # ==================== 文件扫描配置 ====================
    ARKTS_EXTENSIONS: List[str] = [".ets"]  # ArkTS 文件扩展名列表
    
    # ==================== 嵌入向量配置 ====================
    EMBEDDING_DIMENSION: int = 1024  # 嵌入向量维度（根据模型调整）
    
    # ==================== LLM 过滤配置 ====================
    LLM_TEMPERATURE: float = 0.1  # LLM 温度参数（较低值使结果更确定）
    LLM_MAX_TOKENS: int = 2048  # LLM 最大生成 token 数
    LLM_CONTEXT_SIZE: int = 8192  # LLM 上下文窗口大小（num_ctx，默认 8192，即 8k，适合 7b 模型）
    
    # ==================== 搜索结果限制配置 ====================
    MAX_TOTAL_RESULTS: int = 25  # 全局最多返回的函数位置数（保证完整性，避免遗漏）
    
    # ==================== 摘要生成配置 ====================
    MAX_SUMMARY_RETRIES: int = 3  # 摘要生成不完整时的最大重试次数
    
    # ==================== 输出目录配置 ====================
    OUTPUT_DIR: Optional[Path] = None  # 输出目录（默认设置为 test_output/ 目录）
    
    # ==================== 缓存配置 ====================
    CACHE_EMBEDDINGS: bool = True  # 是否缓存嵌入向量
    CACHE_SUMMARIES: bool = True  # 是否缓存文件摘要
    ENABLE_AUDIT_LOGS: bool = False  # 是否启用 LLM 审计日志（用于调试）
    MODEL_CACHE_DIR: Path = Path("/home/dataset/xiebang")  # 模型缓存目录（模型文件，不放在输出目录）
    
    # ==================== 文件保存配置 ====================
    SAVE_NEW_VERSION: bool = False  # 是否每次保存时创建新的时间戳文件（保留历史版本）
    
    # 内部状态
    _loaded = False
    _config_dir = Path(__file__).parent.parent.parent / "configs"
    
    @classmethod
    def load_from_yaml(cls, config_file: Optional[Path] = None) -> bool:
        """
        从 YAML 文件加载配置。
        
        Args:
            config_file: YAML 配置文件路径。如果为 None，则从 configs/ 目录查找。
        
        Returns:
            是否成功加载配置
        """
        if not YAML_AVAILABLE:
            logger.warning("PyYAML not available, skipping YAML config loading")
            return False
        
        # 确定配置文件路径
        if config_file is None:
            # 查找 configs 目录下的 YAML 文件
            if not cls._config_dir.exists():
                logger.debug(f"Config directory not found: {cls._config_dir}")
                return False
            
            # 查找所有 YAML 文件
            yaml_files = list(cls._config_dir.glob("*.yaml")) + list(cls._config_dir.glob("*.yml"))
            if not yaml_files:
                logger.debug(f"No YAML config files found in {cls._config_dir}")
                return False
            
            # 使用第一个找到的 YAML 文件
            config_file = yaml_files[0]
            logger.info(f"Found config file: {config_file}")
        
        if not config_file.exists():
            logger.warning(f"Config file not found: {config_file}")
            return False
        
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config_data = yaml.safe_load(f)
            
            if not config_data:
                logger.warning(f"Config file is empty: {config_file}")
                return False
            
            # 更新配置项
            for key, value in config_data.items():
                if hasattr(cls, key):
                    # 处理路径类型
                    if 'DIR' in key or 'PATH' in key or key == 'OUTPUT_DIR':
                        if isinstance(value, str):
                            value = Path(value)
                    # 处理列表类型
                    elif key == 'ARKTS_EXTENSIONS' and isinstance(value, list):
                        pass  # 保持列表类型
                    # 设置配置值
                    setattr(cls, key, value)
                    logger.debug(f"Loaded config: {key} = {value}")
                else:
                    logger.warning(f"Unknown config key: {key}")
            
            cls._loaded = True
            logger.info(f"Successfully loaded config from {config_file}")
            return True
            
        except Exception as e:
            logger.error(f"Error loading config from {config_file}: {e}")
            return False
    
    @classmethod
    def load_from_env(cls):
        """
        从环境变量加载配置。
        
        环境变量命名规则：FUNCTION_LOCATOR_<CONFIG_KEY>
        例如：FUNCTION_LOCATOR_OLLAMA_HOST
        """
        env_prefix = "FUNCTION_LOCATOR_"
        
        # 配置项到类型的映射
        int_configs = {
            'TOP_K_FILES', 'EMBEDDING_DIMENSION', 'LLM_MAX_TOKENS',
            'MAX_TOTAL_RESULTS', 'MAX_SUMMARY_RETRIES', 'LLM_CONTEXT_SIZE'
        }
        float_configs = {'LLM_TEMPERATURE'}
        bool_configs = {'CACHE_EMBEDDINGS', 'CACHE_SUMMARIES', 'SAVE_NEW_VERSION', 'ENABLE_AUDIT_LOGS'}
        path_configs = {
            'OUTPUT_DIR', 'MODEL_CACHE_DIR'
        }
        
        for key in dir(cls):
            if key.startswith('_') or not key.isupper():
                continue
            
            env_key = f"{env_prefix}{key}"
            env_value = os.getenv(env_key)
            
            if env_value is None:
                continue
            
            try:
                # 根据类型转换
                if key in int_configs:
                    value = int(env_value)
                elif key in float_configs:
                    value = float(env_value)
                elif key in bool_configs:
                    value = env_value.lower() in ('true', '1', 'yes', 'on')
                elif key in path_configs:
                    value = Path(env_value)
                elif key == 'ARKTS_EXTENSIONS':
                    # 支持逗号分隔的列表
                    value = [ext.strip() for ext in env_value.split(',')]
                else:
                    value = env_value
                
                setattr(cls, key, value)
                logger.info(f"Loaded from env: {key} = {value}")
                
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid env value for {key}: {env_value}, error: {e}")
    
    @classmethod
    def load(cls, config_file: Optional[Path] = None):
        """
        加载配置（从环境变量和 YAML 文件）。
        
        配置优先级：环境变量 > YAML 文件 > 默认值
        
        Args:
            config_file: YAML 配置文件路径（可选）
        """
        # 1. 先加载环境变量（优先级最高）
        cls.load_from_env()
        
        # 2. 再加载 YAML 文件（会被环境变量覆盖）
        cls.load_from_yaml(config_file)
    
    @classmethod
    def set_output_dir(cls, output_dir: Path):
        """设置输出目录。"""
        cls.OUTPUT_DIR = output_dir
        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def get_embeddings_cache_dir(cls) -> Path:
        """获取嵌入向量缓存目录。"""
        if cls.OUTPUT_DIR:
            return cls.OUTPUT_DIR / "embeddings_cache"
        return Path("test_output/embeddings_cache")
    
    @classmethod
    def get_summaries_cache_dir(cls) -> Path:
        """获取文件摘要缓存目录。"""
        if cls.OUTPUT_DIR:
            return cls.OUTPUT_DIR / "summaries"
        return Path("test_output/summaries")
    
    @classmethod
    def ensure_directories(cls):
        """确保所有必要的目录存在。"""
        # 确保输出目录存在
        output_dir = cls.OUTPUT_DIR or Path("test_output")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 确保嵌入向量缓存目录存在（如果需要）
        if cls.CACHE_EMBEDDINGS:
            cls.get_embeddings_cache_dir().mkdir(parents=True, exist_ok=True)
        
        # 确保摘要缓存目录存在（如果需要）
        if cls.CACHE_SUMMARIES:
            cls.get_summaries_cache_dir().mkdir(parents=True, exist_ok=True)


# 自动加载配置（如果存在）
# 在模块导入时自动尝试加载配置
Config.load()


# ==================== 功能说明 ====================
"""
Config 类功能说明：

1. 配置加载机制
   - 支持从 YAML 文件读取配置（configs/*.yaml）
   - 支持从环境变量读取配置（FUNCTION_LOCATOR_*）
   - 配置优先级：环境变量 > YAML 文件 > 默认值
   - 使用 Config.load() 手动加载，或依赖自动加载

2. 模型配置
   - OLLAMA_HOST: Ollama API 服务地址，用于调用本地或远程的 Ollama 服务
   - OLLAMA_EMBEDDING_MODEL: 嵌入模型名称，用于将文本转换为向量
   - OLLAMA_LLM_MODEL: 大语言模型名称，用于生成摘要和过滤函数
   - LLM_CONTEXT_SIZE: LLM 上下文窗口大小（num_ctx），默认 8192，可根据模型和需求调整

3. 搜索配置
   - TOP_K_FILES: 相似度搜索返回的文件数量，影响定位精度和速度
   - MAX_TOTAL_RESULTS: 全局最多返回的函数位置数，避免结果过多

4. 文件处理配置
   - ARKTS_EXTENSIONS: 支持的文件扩展名，目前只处理 .ets 文件
   - MAX_SUMMARY_RETRIES: 摘要生成失败时的重试次数，提高成功率

5. 缓存配置
   - CACHE_EMBEDDINGS: 是否缓存嵌入向量，避免重复计算
   - CACHE_SUMMARIES: 是否缓存文件摘要，加速重复分析
   - ENABLE_AUDIT_LOGS: 是否启用 LLM 审计日志（用于调试，默认关闭）
   - MODEL_CACHE_DIR: 模型缓存目录，存储下载的模型文件（不放在输出目录）

6. 输出配置
   - OUTPUT_DIR: 主输出目录，存储所有输出（默认: test_output）
   - SAVE_NEW_VERSION: 是否每次保存时创建新版本，保留历史记录

7. 工具方法
   - load(): 加载配置（从环境变量和 YAML 文件）
   - load_from_yaml(): 从 YAML 文件加载配置
   - load_from_env(): 从环境变量加载配置
   - set_output_dir(): 动态设置输出目录
   - get_embeddings_cache_dir(): 获取嵌入向量缓存目录路径
   - get_summaries_cache_dir(): 获取摘要缓存目录路径
   - ensure_directories(): 确保所有必要的目录都存在

使用示例：

1. 使用默认配置：
    from function_locator import Config
    # 配置已自动加载

2. 从 YAML 文件加载：
    Config.load_from_yaml(Path("./configs/my_config.yaml"))

3. 从环境变量加载：
    export FUNCTION_LOCATOR_OLLAMA_HOST="http://192.168.1.100:11500"
    export FUNCTION_LOCATOR_MAX_TOTAL_RESULTS=50
    Config.load_from_env()

4. 手动设置配置：
    Config.OLLAMA_HOST = "http://192.168.1.100:11500"
    Config.MAX_TOTAL_RESULTS = 50

5. YAML 配置文件示例（configs/config.yaml）：
    OLLAMA_HOST: "http://localhost:11500"
    OLLAMA_EMBEDDING_MODEL: "qwen3-embedding:8b"
    OLLAMA_LLM_MODEL: "qwen2.5-coder:7b"  # 已替换为更小的模型（显存需求从 17.3GB 降至 4.4GB）
    LLM_CONTEXT_SIZE: 8192  # 上下文窗口大小，7b 模型建议使用 8k（可根据模型调整：4096, 8192, 16384）
    TOP_K_FILES: 5
    MAX_TOTAL_RESULTS: 25
    OUTPUT_DIR: "test_output"
"""
