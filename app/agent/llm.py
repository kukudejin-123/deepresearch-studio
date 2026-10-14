"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象
后续主智能体和子智能体都从这里导入 model，避免在多个文件里重复加载环境变量
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

from app.utils.logging_config import get_logger

# find_dotenv 会从当前目录向上查找 .env，适合脚本和 Web 服务从不同入口启动的场景
load_dotenv(find_dotenv())

logger = get_logger(__name__)

# 使用 OpenAI 兼容协议接入模型；具体模型名由 .env 中的 LLM_QWEN_MAX 控制
_model_name = os.getenv("LLM_QWEN_MAX")
if not _model_name:
    # 缺失模型名时 init_chat_model 只会在 deepagents 组装阶段抛出含义不明的
    # TypeError，这里在入口处直接失败，给出可操作的提示
    raise ValueError(
        "未配置 LLM_QWEN_MAX 环境变量：请在 .env 中指定 OpenAI 兼容模型名"
        "（例如 qwen-max），可参考 .env.example"
    )

model = init_chat_model(
    model=_model_name,
    model_provider="openai",
)

logger.info("对话模型已初始化: %s", _model_name)
