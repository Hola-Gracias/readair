"""ReadAir V1 五层实现模块。

- normalizer  : Layer 1 接入标准化层
- gatekeeper  : Layer 2 命中判定层
- debouncer   : Layer 3 防抖聚合层
- storage     : Layer 4 上下文 / 存储层
- executor    : Layer 5 回复执行层

辅助：
- models          : NormalizedInboundMessage / CandidateTask / DebounceWindow
- history_render  : 注入版 History 渲染
"""

from .models import CandidateTask, DebounceWindow, NormalizedInboundMessage

__all__ = [
    "CandidateTask",
    "DebounceWindow",
    "NormalizedInboundMessage",
]
