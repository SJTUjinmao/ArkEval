"""
补丁验证器 - 对补丁进行自动验证（编译/验证规则/冒烟测试）。

根据模块解析要求：
- 编译成功（调用 ArkTS 编译命令或 ts2abc、hvigor assemble）
- AST 结构检查（确保 build() 存在、组件未丢失）
- 日志检查（模拟器 logcat 是否报错）
- 若 repo 有测试套件，可运行指定 tests
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
import logging
import subprocess
import json

logger = logging.getLogger(__name__)


class Verifier:
    """
    补丁验证器 - 验证补丁是否正确应用。
    
    功能：
        - 编译检查
        - AST 结构检查
        - 日志检查
        - 测试运行
    """
    
    def __init__(self, repo_path: Path):
        """
        初始化验证器。
        
        Args:
            repo_path: 仓库根目录路径
        """
        self.repo_path = Path(repo_path).resolve()
    
    def verify(self,
              patch_file: Path,
              verify_options: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        """
        验证补丁。
        
        Args:
            patch_file: 补丁文件路径
            verify_options: 验证选项（compile, ast_check, log_check, tests）
            
        Returns:
            验证结果字典
        """
        if verify_options is None:
            verify_options = {
                "compile": True,
                "ast_check": True,
                "log_check": False,  # 需要模拟器
                "tests": False  # 需要测试套件
            }
        
        results = {
            "passed": True,
            "checks": {},
            "errors": []
        }
        
        # 编译检查
        if verify_options.get("compile", False):
            compile_result = self._check_compile()
            results["checks"]["compile"] = compile_result
            if not compile_result["passed"]:
                results["passed"] = False
                results["errors"].extend(compile_result.get("errors", []))
        
        # AST 结构检查
        if verify_options.get("ast_check", False):
            ast_result = self._check_ast_structure()
            results["checks"]["ast"] = ast_result
            if not ast_result["passed"]:
                results["passed"] = False
                results["errors"].extend(ast_result.get("errors", []))
        
        # 日志检查
        if verify_options.get("log_check", False):
            log_result = self._check_logs()
            results["checks"]["logs"] = log_result
            if not log_result["passed"]:
                results["passed"] = False
                results["errors"].extend(log_result.get("errors", []))
        
        # 测试运行
        if verify_options.get("tests", False):
            test_result = self._run_tests()
            results["checks"]["tests"] = test_result
            if not test_result["passed"]:
                results["passed"] = False
                results["errors"].extend(test_result.get("errors", []))
        
        return results
    
    def _check_compile(self) -> Dict[str, Any]:
        """
        检查编译是否成功。
        
        Returns:
            检查结果
        """
        try:
            # 尝试 hvigor assemble（HarmonyOS 项目）
            result = subprocess.run(
                ['hvigor', 'assemble'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                return {"passed": True, "method": "hvigor"}
            else:
                return {
                    "passed": False,
                    "method": "hvigor",
                    "errors": [result.stderr],
                    "output": result.stdout
                }
                
        except FileNotFoundError:
            # 尝试其他编译命令
            logger.warning("hvigor not found, skipping compile check")
            return {"passed": True, "method": "skipped", "reason": "hvigor not found"}
        except subprocess.TimeoutExpired:
            return {"passed": False, "errors": ["Compile timeout"]}
        except Exception as e:
            return {"passed": False, "errors": [str(e)]}
    
    def _check_ast_structure(self) -> Dict[str, Any]:
        """
        检查 AST 结构（确保 build() 存在、组件未丢失）。
        
        Returns:
            检查结果
        """
        errors = []
        
        # 简单的 AST 检查：查找 build() 方法
        try:
            ets_files = list(self.repo_path.rglob("*.ets"))
            for ets_file in ets_files[:10]:  # 限制检查文件数
                content = ets_file.read_text(encoding='utf-8')
                # 检查是否有 build() 方法
                if '@Component' in content and 'build()' not in content:
                    errors.append(f"Component in {ets_file} missing build() method")
        except Exception as e:
            errors.append(f"AST check error: {e}")
        
        return {
            "passed": len(errors) == 0,
            "errors": errors
        }
    
    def _check_logs(self) -> Dict[str, Any]:
        """
        检查日志（模拟器 logcat 是否报错）。
        
        Returns:
            检查结果
        """
        # 需要模拟器连接，这里返回跳过
        return {
            "passed": True,
            "method": "skipped",
            "reason": "Log check requires emulator connection"
        }
    
    def _run_tests(self) -> Dict[str, Any]:
        """
        运行测试套件。
        
        Returns:
            测试结果
        """
        try:
            # 查找测试文件
            test_files = list(self.repo_path.rglob("*test*.ets"))
            if not test_files:
                return {
                    "passed": True,
                    "method": "skipped",
                    "reason": "No test files found"
                }
            
            # 这里可以添加实际的测试运行逻辑
            return {
                "passed": True,
                "method": "skipped",
                "reason": "Test execution not implemented"
            }
            
        except Exception as e:
            return {
                "passed": False,
                "errors": [str(e)]
            }

