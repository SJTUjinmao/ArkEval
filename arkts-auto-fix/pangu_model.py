"""
Pangu 模型封装模块 - 用于替换 Ollama API 调用
"""

import os
import sys
import logging
from typing import Optional
import torch

logger = logging.getLogger(__name__)

# 设置环境变量
DEFAULT_CACHE_DIR = os.environ.get('MODELSCOPE_CACHE', '/home/dataset/xiebang')
os.environ.setdefault('MODELSCOPE_CACHE', DEFAULT_CACHE_DIR)
os.environ.setdefault('HF_HOME', DEFAULT_CACHE_DIR)
os.environ.setdefault('TRANSFORMERS_CACHE', DEFAULT_CACHE_DIR)
os.environ.setdefault('HUGGINGFACE_HUB_CACHE', DEFAULT_CACHE_DIR)
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

# 尝试导入 transformers
try:
    from transformers import AutoModelForCausalLM, AutoTokenizer
    USE_MODELSCOPE = False
    
    try:
        from modelscope import AutoModel, AutoTokenizer as MSTokenizer
        USE_MODELSCOPE = True
    except ImportError:
        pass
except ImportError as e:
    logger.error(f"缺少必要的依赖库: {e}")
    raise


class PanguModel:
    """Pangu 模型封装类"""
    
    _instance = None
    _model = None
    _tokenizer = None
    _device = None
    _model_path = None
    
    def __init__(self, model_path: str = "/opt/pangu/openPangu-Embedded-7B-V1.1", use_modelscope: bool = None):
        """
        初始化 Pangu 模型
        
        Args:
            model_path: 模型路径
            use_modelscope: 是否使用 ModelScope（None 表示自动检测）
        """
        self.model_path = model_path
        self.use_modelscope = use_modelscope if use_modelscope is not None else USE_MODELSCOPE
        
        # 检测设备
        if hasattr(torch, "npu") and torch.npu.is_available():
            self.device = "npu"
        elif hasattr(torch, "cuda") and torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        
        # 单例模式：如果已经加载过相同路径的模型，直接使用
        if PanguModel._instance is not None and PanguModel._model_path == model_path:
            self.model = PanguModel._model
            self.tokenizer = PanguModel._tokenizer
            self.device = PanguModel._device
            logger.info(f"复用已加载的模型: {model_path}")
        else:
            self.load_model()
            PanguModel._instance = self
            PanguModel._model = self.model
            PanguModel._tokenizer = self.tokenizer
            PanguModel._device = self.device
            PanguModel._model_path = model_path
    
    def load_model(self):
        """加载模型"""
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"模型路径不存在: {self.model_path}")
        
        logger.info(f"加载 Pangu 模型: {self.model_path}, 设备: {self.device}")
        
        try:
            if self.use_modelscope:
                self.tokenizer = MSTokenizer.from_pretrained(
                    self.model_path,
                    trust_remote_code=True,
                    cache_dir=DEFAULT_CACHE_DIR
                )
                
                from modelscope import AutoModelForCausalLM
                device_map = "npu" if self.device == "npu" else ("auto" if self.device == "cuda" else None)
                
                if self.device == "npu":
                    os.environ["TORCH_NPU_DISABLE_FUSED_ATTENTION"] = "1"
                
                torch_dtype = None
                if self.device == "npu":
                    try:
                        torch_dtype = torch.bfloat16
                    except:
                        torch_dtype = torch.float16
                elif self.device == "cuda":
                    torch_dtype = torch.float16
                
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.model_path,
                    device_map=device_map,
                    torch_dtype=torch_dtype,
                    trust_remote_code=True,
                    cache_dir=DEFAULT_CACHE_DIR
                )
                
                if device_map is None:
                    self.model = self.model.to(self.device)
                    if torch_dtype is not None:
                        self.model = self.model.to(torch_dtype)
                else:
                    if torch_dtype is not None:
                        model_dtype = next(self.model.parameters()).dtype
                        if model_dtype == torch.float32:
                            try:
                                self.model = self.model.to(torch_dtype)
                            except Exception as e:
                                logger.warning(f"转换精度失败: {e}")
                
                self.model.eval()
            else:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    self.model_path,
                    trust_remote_code=True,
                    cache_dir=DEFAULT_CACHE_DIR
                )
                
                from transformers import AutoModelForCausalLM as HFModel
                dtype = torch.float16 if self.device in ["cuda", "npu"] else torch.float32
                device_map = "auto" if self.device == "cuda" else ("npu" if self.device == "npu" else None)
                
                if self.device == "npu":
                    os.environ["TORCH_NPU_DISABLE_FUSED_ATTENTION"] = "1"
                
                original_env = os.environ.get("NPU_VISIBLE_DEVICES", None)
                if self.device == "cpu":
                    os.environ["NPU_VISIBLE_DEVICES"] = ""
                    os.environ["TORCH_NPU_DISABLE_FUSED_ATTENTION"] = "1"
                
                try:
                    self.model = HFModel.from_pretrained(
                        self.model_path,
                        dtype=dtype,
                        device_map=device_map,
                        trust_remote_code=True,
                        cache_dir=DEFAULT_CACHE_DIR,
                        local_files_only=True
                    )
                finally:
                    if self.device == "cpu":
                        if original_env is None:
                            os.environ.pop("NPU_VISIBLE_DEVICES", None)
                        else:
                            os.environ["NPU_VISIBLE_DEVICES"] = original_env
                
                if device_map is None:
                    self.model = self.model.to(self.device)
                
                self.model.eval()
            
            logger.info("Pangu 模型加载成功")
            
        except Exception as e:
            logger.error(f"Pangu 模型加载失败: {e}")
            raise
    
    def generate(self, prompt: str, max_new_tokens: int = 512, temperature: float = 0.7, 
                 max_length: int = 2048) -> str:
        """
        生成回答
        
        Args:
            prompt: 输入提示
            max_new_tokens: 最大生成 token 数
            temperature: 温度参数
            max_length: 最大输入长度
            
        Returns:
            生成的文本
        """
        if self.model is None or self.tokenizer is None:
            raise Exception("模型未加载，请先调用 load_model()")
        
        try:
            # 编码输入
            inputs = self.tokenizer(prompt, return_tensors="pt", padding=True, 
                                   truncation=True, max_length=max_length)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
            # 生成回答
            with torch.no_grad():
                if self.device == "npu":
                    os.environ["TORCH_NPU_DISABLE_FUSED_ATTENTION"] = "1"
                    model_dtype = next(self.model.parameters()).dtype
                    if model_dtype == torch.float32:
                        try:
                            self.model = self.model.to(torch.bfloat16)
                        except:
                            try:
                                self.model = self.model.to(torch.float16)
                            except:
                                pass
                
                outputs = self.model.generate(
                    inputs["input_ids"],
                    attention_mask=inputs.get("attention_mask", None),
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    do_sample=temperature > 0,
                    pad_token_id=self.tokenizer.eos_token_id if self.tokenizer.eos_token_id is not None else self.tokenizer.pad_token_id
                )
            
            # 解码输出
            response = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
            
            # 提取生成的部分（移除输入部分）
            if prompt in response:
                response = response.split(prompt)[-1].strip()
            
            return response
            
        except Exception as e:
            logger.error(f"生成回答失败: {e}")
            raise

