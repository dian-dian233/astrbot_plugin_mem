"""文本归一化：**纯标准库，不 import astrbot**。

拆出来单独一个模块，是因为 `main.py`（有 astrbot 依赖）和 `livingmemory.py`（要能脱离
框架直接跑）都要用同一份归一化规则 —— 两边各抄一份迟早会漂。

规则和 v0.2.0 完全一致：
- 全角空格 / 不换行空格 / 各类窄空格 → 半角空格
- 连续空白压成一个
- 中文标点一律不动（「，。」是用户写法，改了反而检索不到）
"""

from __future__ import annotations

import re

# 归一化要压掉的空白（全角空格、不换行空格、各类窄空格 + 常规空白）
SPACE_CHARS = ("\u3000", "\u00a0", "\u2002", "\u2003", "\u2009", "\u202a", "\u202f")

_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """写入归一化：特殊空格→半角，连续空白压成一个，去首尾空白。"""
    s = str(text or "")
    for ch in SPACE_CHARS:
        s = s.replace(ch, " ")
    return _SPACES.sub(" ", s).strip()
