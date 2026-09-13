# core/os_layer/safety.py
"""
OS 层安全管理。

职责：
1. 授权作用域（会话级"始终允许"，risk=3 永远单独确认不受此影响）
2. 步数 / replan 计数器

注：这里【曾经】有过第三种停止方式——"软急停"（用户说"别动我的屏幕"→ 关键词/LLM
判定 → 置 _aborted 标志 → dispatch 拒绝一切后续动作）。实际使用后发现，只要
甩鼠标 failsafe 和 Ctrl+` 热键这两种在，没有正常人会去用第三种；而它自己带着一个
很坏的性质：_aborted 是进程级且没有任何地方会复位，一旦触发这个进程就再也不能操作
电脑，连"重置当前对话"都解不开。所以整套软急停已删除，只保留 EmergencyStop
（core/os_layer/executor_action.py）那两种真正在用的。

设计原则（实现约束 2）：
- safety.py 只管"允不允许"，不管"怎么执行"。
- 决策权留给上层（_handle_os_task），这里只暴露查询和修改接口。

risk=3 永远单独确认：即使用户选了"始终允许 risk=2"，risk=3 仍必须每次弹窗。
这条写死在 is_pre_authorized 里，不受任何配置影响。
"""
from __future__ import annotations
import threading
from typing import Set, Tuple
from loguru import logger


class OSSessionSafety:
    """单次对话会话的 OS 安全状态。对话重置时应重新实例化（或调 reset()）。"""

    def __init__(self):
        self._lock = threading.Lock()
        # 用户授权"本次对话始终允许"的 (action, scope_key) 元组集合（仅 risk≤2 有效）
        # scope_key 用于区分同一 action 的不同参数范围，防止"允许写A文件"扩散到"允许写B文件"
        self._pre_authorized: Set[Tuple[str, str]] = set()
        # 步数计数
        self._step_count = 0
        # replan 计数
        self._replan_count = 0

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def reset(self):
        """对话重置时清空所有状态。

        必须被调用：pre_authorize 的语义是"【本次对话】始终允许"，
        如果重置对话后授权还在，那个 scope 就名不副实了。
        接线点在 Orchestrator.reset_conversation()。
        """
        with self._lock:
            self._pre_authorized.clear()
            self._step_count = 0
            self._replan_count = 0
        logger.info("[OS-Safety] 已随对话重置清空预授权与计数。")

    # ── 预授权管理 ────────────────────────────────────────────────────────

    def pre_authorize(self, action: str, risk: int, scope_key: str = "*"):
        """用户选择"本次对话始终允许"时调用。risk=3 静默忽略（不能预授权高危）。

        scope_key 限定授权范围，防止"允许写桌面文件"扩散到"允许写系统目录"：
        - file_write/file_read/file_delete/file_move: 父目录路径
        - open_url: 域名
        - 其他: "*"（会话级通用授权）
        调用方应确保 scope_key 来自实际执行成功后的 resolved_instr。
        """
        if risk >= 3:
            logger.warning(f"[OS-Safety] risk=3 操作 '{action}' 不可预授权，已忽略。")
            return
        with self._lock:
            self._pre_authorized.add((action, scope_key))
        logger.info(f"[OS-Safety] 已预授权 action='{action}' scope='{scope_key}'（risk={risk}）。")

    def is_pre_authorized(self, action: str, risk: int, scope_key: str = "*") -> bool:
        """检查是否已预授权（可跳过本次弹窗确认）。

        规则（铁律，不可配置）：
        - risk=1：只读，永远不需要授权。
        - risk=2：可预授权，命中 (action, scope_key) 或 (action, "*") 返回 True。
        - risk=3：永远返回 False，必须每次单独弹窗确认。
        """
        if risk <= 1:
            return True   # 只读无需授权
        if risk >= 3:
            return False  # 高危永远单独确认
        with self._lock:
            return (action, scope_key) in self._pre_authorized or (action, "*") in self._pre_authorized

    def revoke(self, action: str, scope_key: str | None = None):
        """撤销 action 的预授权。scope_key=None 时撤销该 action 所有范围的授权。"""
        with self._lock:
            if scope_key is not None:
                self._pre_authorized.discard((action, scope_key))
            else:
                self._pre_authorized = {p for p in self._pre_authorized if p[0] != action}

    def revoke_all(self):
        """撤销所有预授权（用户说"停"时调用）。"""
        with self._lock:
            self._pre_authorized.clear()

    # ── 计数器 ─────────────────────────────────────────────────────────

    def increment_step(self) -> int:
        with self._lock:
            self._step_count += 1
            return self._step_count

    def increment_replan(self) -> int:
        with self._lock:
            self._replan_count += 1
            return self._replan_count

    @property
    def step_count(self) -> int:
        with self._lock:
            return self._step_count

    @property
    def replan_count(self) -> int:
        with self._lock:
            return self._replan_count
