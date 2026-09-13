"""上下文治理层。

计量层（2026-08-13 落地）：**只做计量，不做衰减** —— `meter.py`。
📌 为什么先做计量：per-model 配额必须在实际运行中标定，而**标定之前得先能量**；
   且这一片**零行为改变**，不可能引入静默 bug。
阶梯本体（L0→L4 + `decay.py` + `bridge.py` + UI「记忆起点」卡 + 搜索框）在计量之后落地。
"""
from core.context.meter import (  # noqa: F401
    MAIN_REACT, ContextMeter, estimate_request, estimate_text,
    get_meter, normalize_prompt_input,
)
