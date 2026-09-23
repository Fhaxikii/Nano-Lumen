# core/os_layer/canary.py
"""
Canary 自检 —— 定期（仅空闲时）用已知稳定目标测试 VisionLocator UIA 链路是否
健康，趋势退化时主动告警，而不是放任用户自己发现"最近总是定位失败"才察觉。

设计约束：

1. 【目标必须稳定常驻】不能用"记事本是否开着"这种临时窗口当测试目标——目标
   本身就可能没打开，会把"目标不存在"误判成"定位失败"，污染统计。改用 Windows
   任务栏的标准按钮（搜索/任务视图/小组件/通知），系统启动后必然存在，且通过
   VisionLocator.locate_taskbar_canary() 直接定位任务栏窗口（Shell_TrayWnd），
   不依赖 get_target_window() 那套"猜用户想操作哪个窗口"的逻辑。

2. 【只在空闲时跑，不能硬定时器无脑跑】canary 自检本身要做 UIA 定位，如果和
   真实用户任务并发执行，会重新引入"自检抢占前台焦点干扰正在执行的真实任务"
   这个刚修过的同类问题。should_run() 必须同时检查"距上次运行够久"和调用方
   传入的"当前是否有 OS 任务正在执行"，两者都满足才跑。调用方（orchestrator）
   负责维护"OS 任务是否正在执行"这个状态并如实传入，本模块不自己猜。

3. 【不能永远只是被动写日志】连续 N 次成功率低于阈值，要从"写审计日志"升级
   成"主动告知用户"，否则等于把"有没有人定期翻日志"的责任完全甩给人工，机制
   形同虚设。升级判定在 run_once() 里做，返回 escalate 标志，调用方负责真正
   把这条消息推给用户（写 memory，让 Nano 下次对话时能提到）。
"""
from __future__ import annotations
import contextlib
import json
import sys
import time
import pathlib
from typing import Any, Dict, List, Optional
from loguru import logger


@contextlib.contextmanager
def _best_effort_file_lock(lock_path: pathlib.Path, timeout_seconds: float = 2.0):
    """跨进程文件锁，尽力而为：抢不到/拿不到就放弃，绝不让"等锁"拖慢或卡死
    主流程（这是这次加锁唯一需要守住的底线——多标签页/多进程并发写
    canary_state.json 本来就只是"小概率丢一条记录"这种轻量级问题，加锁是
    为了更好，不能因为锁本身的 bug 反而引入"卡死"这种更重的新问题）。

    用 Windows 标准库自带的 msvcrt.locking()，不引入新依赖（这台机器上连
    pywin32 都没装，stdlib 方案更稳）。非 Windows 平台直接跳过，当作"没锁"
    处理——这个模块本来就是 core/os_layer/ 下的 Windows-only 代码。

    yields: bool，是否真的拿到了锁。调用方不管拿没拿到锁都应该继续往下写，
    只是拿到锁时多了一层"不会和别的进程同时写"的保证。
    """
    if sys.platform != "win32":
        yield False
        return
    import msvcrt
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = None
    acquired = False
    try:
        f = open(lock_path, "a+b")
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                time.sleep(0.05)
        yield acquired
    except Exception as e:
        logger.debug(f"[Canary] 文件锁尝试异常（降级为不加锁）: {e}")
        yield False
    finally:
        if acquired and f is not None:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


# 🪦 **写死的目标名单已废弃**（2026-08-28）。原值：
#     ["任务视图", "显示桌面", "操作中心", "系统时钟"]
# 它同时踩了三个坑，而这三个坑我们一个都控制不了：
#     语言   英文 Windows 上叫 Task View / Show desktop / … ⇒ 0/4 全灭
#     版本   Win11 任务栏是 XAML 重写的，控件结构不同
#     布局   用户可以关掉任意一个 —— 实测有的机器就没开「任务视图」，
#            两个月里一直是 3/4，而 75% > 50% 阈值 ⇒ 悄悄地不报警
# ⚠️ 旧注释写着「多语言可能不一样，应该走 config 的 canary.targets」——
#    但 config 里的值也是这四个中文。📌 把问题挪进配置文件不等于解决它。
# ⇒ 改为运行时现场探测：`VisionLocator.discover_taskbar_probes()`。
_PROBE_COUNT_DEFAULT = 4
_STATE_FILENAME = "canary_state.json"


class CanarySelfCheck:
    def __init__(self, vision_locator, state_dir: pathlib.Path,
                 config: Optional[Dict[str, Any]] = None):
        """
        Args:
            vision_locator: VisionLocator 实例（复用 OSDispatcher 已有的那个，
                不要重新构造——避免重复初始化 provider/screenshot_dir）。
            state_dir: 状态文件存放目录，建议传 OS 审计目录（data/os_audit/）。
            config: 来自 os_config.json 的 canary 配置段，缺省用内置默认值。
        """
        self._vision = vision_locator
        self._state_path = state_dir / _STATE_FILENAME
        self._lock_path = state_dir / (_STATE_FILENAME + ".lock")
        cfg = config or {}
        # ⚠️ `cfg["targets"]` **不再被读取**（见上方 _PROBE_COUNT_DEFAULT 处的留痕）。
        #    配置文件里那个键已改名为 `_targets_removed_2026_08_28`，保留原值只为
        #    让下一个人看得见它曾经是什么、以及为什么不用了。
        self._probe_count: int = int(cfg.get("probe_count", _PROBE_COUNT_DEFAULT))
        # 距上次运行至少要隔这么久才会再跑一次（约束1的"定期"具体化，默认30分钟）
        self._interval_seconds: int = int(cfg.get("interval_seconds", 1800))
        # 本轮成功率低于这个值，算"这次低"
        self._success_threshold: float = float(cfg.get("success_threshold", 0.5))
        # 连续多少次"这次低"才升级成主动告知用户（不是写日志了事）
        self._escalate_after: int = int(cfg.get("escalate_after_consecutive_low", 3))
        self._state = self._load_state()

    # ── 状态持久化（JSON 文件，跨重启保留 last_run_ts/连续低次数）───────────

    def _load_state(self) -> Dict[str, Any]:
        try:
            with _best_effort_file_lock(self._lock_path):
                if self._state_path.exists():
                    return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[Canary] 读取状态失败，重新初始化: {e}")
        return {"last_run_ts": 0.0, "history": [], "consecutive_low": 0}

    def _save_state(self) -> None:
        # 多个标签页/多个进程各自跑一个 CanarySelfCheck 实例、共享同一个状态
        # 文件时，加这把锁防止两边同时 write_text 互相截断对方写到一半的内容。
        # 锁是尽力而为（见 _best_effort_file_lock），抢不到也照样写，最坏情况
        # 退化回"没加锁"那个本来就不严重的旧问题，不会因为加锁反而卡死写入。
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with _best_effort_file_lock(self._lock_path) as got_lock:
                if not got_lock:
                    logger.debug("[Canary] 状态文件锁未拿到，仍直接写（最坏情况丢一条记录）")
                self._state_path.write_text(
                    json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as e:
            logger.warning(f"[Canary] 保存状态失败: {e}")

    # ── 触发判定（约束2）─────────────────────────────────────────────────

    def should_run(self, os_task_busy: bool) -> bool:
        """是否应该跑一轮自检。

        Args:
            os_task_busy: 调用方如实传入"当前这台电脑是否有人在开"。
                True 时永远不跑，不管距上次运行隔了多久——宁可错过一次检查窗口，
                也不抢真实任务的前台焦点。

        ⚠️ **本模块仍然不自己猜**（约束 2 的原话），但从 2026-08-07 起
        调用方的数据来源换了：不再是 orchestrator 上的一个裸 bool，
        而是 `oslease.machine_is_free()`。区别不在写法，在**过期语义**——
        裸 bool 漏写一次 False 就永久停摆且一声不响（实测停了 61 分钟）；
        租约到点自动不算数，**不依赖任何调用点记得配对**。
        ⭐ 附带扩了语义：现在"用户正在用电脑"也算忙。旧 bool 表达不了这件事，
        所以旧实现下用户打字时 canary 照样会跳出来抢焦点。
        """
        if os_task_busy:
            return False
        elapsed = time.time() - float(self._state.get("last_run_ts", 0.0))
        return elapsed >= self._interval_seconds

    # ── 执行一轮自检 ─────────────────────────────────────────────────────

    async def run_once(self) -> Dict[str, Any]:
        """对每个稳定目标做一次只读 UIA 定位测试（不点击，无副作用）。

        Returns:
            {"rate": 本轮成功率, "results": [...], "escalate": bool,
             "consecutive_low": int}
        """
        # ── 第 1 步：先探测【现在到底有什么】────────────────────────────
        # 📌 2026-08-28 定：「先探测用户目前有什么控件存在，再进行自检」。
        #    这两步分开之后，「控件没开」就再也不会被算成「定位能力坏了」。
        try:
            probes = await self._vision.discover_taskbar_probes(self._probe_count)
        except Exception as e:
            logger.warning(f"[Canary] 探测探针失败: {e}")
            probes = []

        if not probes:
            # 🔴 **探不到 ≠ 成功率 0%。** 这一支存在的全部理由就是不让这两件事
            #    再混在一起 —— 改造前 `rate = success/len(results) if results else 0.0`
            #    正是把「测不了」直接写成了「0 分」。
            # ⚠️ 也**不动** consecutive_low：这次根本没测，既不该算失败，
            #    也不该把之前累计的失败一笔勾销。
            self._state["last_run_ts"] = time.time()
            self._save_state()
            logger.debug("[Canary] 本轮跳过：任务栏上没有名字稳定的控件")
            return {"rate": None, "undetectable": True, "results": [],
                    "escalate": False,
                    "consecutive_low": self._state.get("consecutive_low", 0)}

        # ── 第 2 步：拿现场探到的探针做定位测试 ──────────────────────────
        results = []
        for target in probes:
            t0 = time.time()
            try:
                res = await self._vision.locate_taskbar_canary(target)
                status = res.get("status", "ERROR")
            except Exception as e:
                logger.warning(f"[Canary] 定位「{target}」异常: {e}")
                status = "ERROR"
            results.append({
                "target": target, "status": status,
                "latency_ms": int((time.time() - t0) * 1000),
            })

        success = sum(1 for r in results if r["status"] == "SUCCESS")
        rate = success / len(results) if results else 0.0
        now = time.time()

        is_low = rate < self._success_threshold
        self._state["consecutive_low"] = (self._state.get("consecutive_low", 0) + 1) if is_low else 0

        self._state["last_run_ts"] = now
        history = self._state.setdefault("history", [])
        history.append({"ts": now, "rate": rate, "results": results,
                        "consecutive_low": self._state["consecutive_low"]})
        self._state["history"] = history[-50:]  # 只留最近50次，防止状态文件无限增长
        self._save_state()

        _escalate = self._state["consecutive_low"] >= self._escalate_after
        # 自检通过或尚未达到升级条件时只记 DEBUG；达到升级条件时由调用方告知用户，这里记 WARNING。
        (logger.warning if _escalate else logger.debug)(
            f"[Canary] 视觉定位自检成功率 {rate:.0%}（{success}/{len(results)}），"
            f"连续低于阈值 {self._state['consecutive_low']} 次"
        )

        return {
            "rate": rate, "results": results, "undetectable": False,
            "escalate": self._state["consecutive_low"] >= self._escalate_after,
            "consecutive_low": self._state["consecutive_low"],
        }
