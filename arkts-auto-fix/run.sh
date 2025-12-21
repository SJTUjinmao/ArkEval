#!/bin/bash
###############################################################################
# 一键启动完整工作流脚本
# 
# 使用方法：
#   ./run.sh "问题描述"
#   或
#   ./run.sh --file example_problem.txt
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# 默认配置
REPO_PATH="${SCRIPT_DIR}/repos/Homogram"
PANGU_MODEL_PATH="/opt/pangu/openPangu-Embedded-7B-V1.1"
OUTPUT_DIR="test_output"

# 颜色定义
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 解析参数
PROBLEM=""
if [[ "$1" == "--file" || "$1" == "-f" ]]; then
    if [[ -f "$2" ]]; then
        PROBLEM=$(cat "$2")
        echo -e "${BLUE}从文件读取问题描述: $2${NC}"
    else
        echo "错误: 文件不存在: $2"
        exit 1
    fi
elif [[ -n "$1" ]]; then
    PROBLEM="$1"
else
    echo "使用方法:"
    echo "  $0 \"问题描述\""
    echo "  $0 --file example_problem.txt"
    exit 1
fi

echo ""
echo -e "${GREEN}=========================================="
echo "  一键启动完整工作流"
echo "==========================================${NC}"
echo ""
echo "问题描述: ${PROBLEM:0:100}..."
echo "仓库路径: ${REPO_PATH}"
echo "Pangu 模型路径: ${PANGU_MODEL_PATH}"
echo "输出目录: ${OUTPUT_DIR}"
echo ""

# 检查仓库路径
if [ ! -d "${REPO_PATH}" ]; then
    echo -e "${YELLOW}警告: 仓库路径不存在: ${REPO_PATH}${NC}"
    echo -e "${YELLOW}请确认仓库路径是否正确${NC}"
    exit 1
fi

# 检查模型路径
if [ ! -d "${PANGU_MODEL_PATH}" ]; then
    echo -e "${YELLOW}警告: 模型路径不存在: ${PANGU_MODEL_PATH}${NC}"
    echo -e "${YELLOW}请确认模型路径是否正确${NC}"
fi

# 清理 Python 缓存
echo -e "${BLUE}清理 Python 缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true
echo -e "${GREEN}✓ 缓存已清理${NC}"
echo ""

# 恢复仓库状态
echo -e "${BLUE}恢复仓库到干净状态...${NC}"
cd "${REPO_PATH}"
git reset --hard HEAD 2>/dev/null || true
git clean -fd 2>/dev/null || true
cd "${SCRIPT_DIR}"
echo -e "${GREEN}✓ 仓库已恢复${NC}"
echo ""

# 运行工作流
echo -e "${GREEN}开始运行完整工作流...${NC}"
echo ""

cd "${SCRIPT_DIR}"

# 检查是否在 conda 环境中
if command -v conda &> /dev/null; then
    # 尝试激活 pangu 环境
    if conda env list | grep -q "pangu"; then
        source "$(conda info --base)/etc/profile.d/conda.sh" 2>/dev/null || true
        conda activate pangu 2>/dev/null || true
    fi
fi

python3 run_full_workflow.py \
    --repo "${REPO_PATH}" \
    --problem "${PROBLEM}" \
    --pangu-model-path "${PANGU_MODEL_PATH}" \
    --apply

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✓ 工作流完成成功！${NC}"
    echo ""
    echo "查看结果:"
    echo "  - 定位结果: ${OUTPUT_DIR}/locator_output.json"
    echo "  - 补丁文件: ${OUTPUT_DIR}/patches/"
    echo ""
    echo "修改的文件:"
    cd "${REPO_PATH}"
    git status --short 2>/dev/null | head -10 || echo "  无修改"
else
    echo -e "${YELLOW}✗ 工作流执行失败 (退出码: ${EXIT_CODE})${NC}"
fi

exit $EXIT_CODE

