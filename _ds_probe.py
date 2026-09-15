"""临时探针：故意引入若干 DeepSource 可检出的问题，用于验证分析链路有效。

验证完成后本文件会被删除。
"""
import os  # PY-W2000 未使用导入
import json  # PY-W2000 未使用导入

import threading  # PY-W2000 未使用导入


DUPLICATE = {
    "alpha": 1,
    "beta": 2,
    "alpha": 3,  # PYL-W0109 重复字典键
}


def probe_lambda():
    return lambda x: x + 1  # FLK-E731 具名 lambda


def probe_overlap(path):
    try:
        return open(path, encoding="utf-8").read()
    except (FileNotFoundError, OSError):  # PYL-W0714 异常重叠
        return ""
