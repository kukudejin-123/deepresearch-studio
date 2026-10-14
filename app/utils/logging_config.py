"""
结构化日志配置模块

统一项目的日志格式、级别和输出目标。
服务入口（FastAPI lifespan）和脚本入口都应在启动时调用一次 setup_logging，
避免各模块自行 print 导致格式混乱、也无法按级别过滤。
"""

import logging
import sys

# 日志格式包含时间、级别、模块名和消息，便于本地排查和后期接入日志收集系统
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """
    初始化全局日志配置

    :param level: 日志级别字符串，例如 DEBUG、INFO、WARNING；非法值回退为 INFO
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        # 重复调用时覆盖已有 root handler，保证 uvicorn reload 等场景格式一致
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """
    获取模块级 logger

    :param name: 通常传入 __name__
    :return: 标准库 Logger 对象
    """
    return logging.getLogger(name)
