# -*- coding: utf-8 -*-
"""GoldDesk 核心引擎（零第三方依赖，仅 Python 标准库）。

Windows 桌面端单端程序的核心引擎：一个进程持有自己的 ConfigStore、DB、
Pipeline、Scheduler 与 SecretsStore，数据全部落在本机。
"""
from .util import DISCLAIMER, DISCLAIMER_MD  # noqa: F401

__version__ = "1.0.0"
PROMPT_VERSION = "v3"
