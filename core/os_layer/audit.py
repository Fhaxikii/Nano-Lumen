# core/os_layer/audit.py
"""
OS 层审计日志。

独立于 working memory（memory_store.py）。区别：
- working memory：给模型 recall 的语义记忆，可被筛选/格式化
- 审计日志：不可篡改的全量操作流水，每个原子动作一行 JSON，用于事后追溯

写入路径：data/os_audit/os_actions.log（append-only，一行一条 JSON）
只读动作也照样写审计 —— "每个 OS 动作都留痕"这条链路不分档位。

实现约束：审计要的是全量流水，不是语义记忆。
"""
from __future__ import annotations
import json
import pathlib
import threading
from datetime import datetime
from typing import Any, Dict, Optional, List
from loguru import logger


class OSAuditLogger:
    _instance: "OSAuditLogger | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, log_dir: Optional[pathlib.Path] = None):
        if log_dir is None:
            root = pathlib.Path(__file__).parent.parent.parent
            log_dir = root / "data" / "os_audit"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / "os_actions.log"
        self._screenshot_dir = log_dir / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._prune_tick = 0
        logger.debug(f"[OS-Audit] 审计日志已就绪: {self._log_path}")

    @property
    def screenshot_dir(self) -> pathlib.Path:
        return self._screenshot_dir

    def record(self,
               action: str,
               params: Dict[str, Any],
               effective_risk: int,
               result_status: str,
               session_id: str = "",
               risk_reasons: Optional[List[str]] = None,
               locate_status: str = "",
               result_summary: str = "",
               aborted: bool = False,
               error: str = "") -> None:
        """写一条审计记录（append-only）。

        Args:
            action:         DSL action 名
            params:         指令参数（敏感字段在写入前应已被调用方脱敏）
            effective_risk: 经地板+动态升级后的最终风险等级
            result_status:  "success" / "failed" / "blocked" / "aborted"
            risk_reasons:   风险等级是怎么算出来的（地板/动态升级原因）
            locate_status:  视觉定位状态（只读动作留空）
            result_summary: 结果摘要（如系统信息的关键值、截图路径）
            aborted:        是否被急停中断
            error:          失败/阻断原因
        """
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "session_id": session_id,
            "action": action,
            "params": self._safe_params(params),
            "effective_risk": effective_risk,
            "risk_reasons": risk_reasons or [],
            "result_status": result_status,
            "locate_status": locate_status,
            "result_summary": result_summary[:500],
            "aborted": aborted,
            "error": error[:300],
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._write_lock:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                # 审计写失败是严重问题，但不能因此中断主流程——记到 loguru 兜底
                logger.error(f"[OS-Audit] 审计写入失败: {e} | entry={line[:200]}")
        # ⭐ 回收挂在这条心跳上 —— 见 `_maybe_prune_screenshots` 上方那段。
        #    ⚠️ 放在锁**外面**：回收要 glob 整个目录，不该占着审计的写锁。
        self._maybe_prune_screenshots()

    # ══════════════════════════════════════════════════════════════════════
    # 截图目录回收 —— 按【体积】，不按张数
    # ══════════════════════════════════════════════════════════════════════
    #
    # 🔴🔴 **这是同一条教训的第三次。** 前两次都在 `executor_low._prune_shots`：
    #    ① 2026-08-23：glob 写成 `shot_*.png`，而真实文件名是 `locate_*.png`
    #       → **一张都没匹配到**，清理跑了等于没跑。
    #    ② 2026-08-25：glob 修对了，但**触发点仍然挂在「保存 shot_* 时」** ——
    #       而 `shot_*` 一共只有 3 张，`locate_*`/`annotated_locate_*` 有 44 张。
    #       实测：47 张 / 52.4 MB，上限写着 20，清理这辈子只跑过 3 次。
    #
    # 📌 ① 的修复处逐字写着「**一个要求所有写入方都自觉的回收，漏一个就永远漏**」，
    #    然后只把**扫描范围**改成按目录，**触发时机**照旧挂在某一个写入方身上。
    #    ⭐ **扫描范围和触发时机是两件事** —— 修了前一件不等于修了这条教训。
    #
    # ⭐ 所以现在挂在 `record()` 上：**每个 OS 动作都会写审计**，这是一条
    #    没有例外的心跳；而目录本来就是审计层自己的（`self._screenshot_dir`）。
    #    📌 **回收该由「拥有这个目录的人」负责，不由「往里写东西的人」负责。**
    #
    # ── 三条规则，各自答一个不同的问题 ──────────────────────────────
    #
    # ① 删最旧的，直到总量 ≤ 预算
    #      审计证据里**新的更有用** —— 你回头查的是刚发生的事。
    #
    # ② 地板：无论如何留最近 `_SHOT_FLOOR` 张
    #      语义是 **「证据 > 磁盘」**：宁可暂时超预算，也不能让目录空到查不了。
    #
    # ③ 单张自己就吃掉预算 `_SHOT_OUTLIER_RATIO` 的 → **它本身就是异常**，
    #    优先删，且**不受 ② 保护**。
    #      🔴 只有 ①② 的话，那个场景是反的：9 张 1MB 的好证据 +
    #         1 张 200MB 的异常 → 按「删最旧」会**删掉 9 张好的、留下那张坏的**。
    #      📌 正常截图不可能那么大 —— 它不但在吃预算，**它作为证据本身也是坏的**。
    #         留一个坏证据去挤掉好证据，两头都亏。
    #      ⚠️ 门槛 20MB vs 实测单张中位 1.08MB / 最大 1.86MB —— **十倍以上余量**，
    #         不会误伤正常截图。
    #
    # ── 为什么不按张数（2026-08-25 定，同记忆水位那条判据）──────────
    # 📌 **这个数的语义不是「最多占用多少」，是「一个长期使用 Nano 的用户，
    #    要永久让出多少磁盘」** —— 它只增不减，长期用就一定停在这个值上。
    #    也就是 README 里那行「推荐预留空间」的实际取值。
    # ⇒ 而「20 张」写不出那一行：20 张可能是 4MB，也可能是 200MB。
    #    「20 张和 21 张的语义差别是什么？」——没有差别，那个数不约束任何东西。
    # ⚠️ 当初给「按张数」编的理由是：「按体积要每次统计整个目录（O(n)）」——
    #    **那是假的**：排序本来就要对每个文件调 `stat()`，而 `st_size`
    #    和 `st_mtime` 来自**同一次** `stat()`。体积是白拿的，零额外 IO。
    #    📌 那是先选了做法、再去给它编一个理由，而那个理由经不起看一眼下一行代码。

    _SHOT_BUDGET_BYTES = 200 * 1024 * 1024   # 200 MB
    _SHOT_FLOOR = 10                          # 一次 GUI 定位任务约 2-4 张
    _SHOT_OUTLIER_RATIO = 0.10                # 单张 > 20MB 视为异常
    _PRUNE_EVERY_N_RECORDS = 20               # 不是每条审计都扫目录

    def _maybe_prune_screenshots(self) -> None:
        """审计心跳：每 N 条记录扫一次截图目录。

        ⚠️ 节流是为了别让每个 OS 动作都付一次 `glob`+`stat` 的钱；
           📌 但节流**不能**让回收变成「可能永远不跑」—— N 条一定会到，
              而上一版的触发条件（「有人保存 shot_*」）**可能一次都不到**。
              这就是节流和「挂在某个写入方身上」的区别。
        """
        self._prune_tick += 1
        if self._prune_tick % self._PRUNE_EVERY_N_RECORDS:
            return
        try:
            self.prune_screenshots()
        except Exception as e:
            # ⚠️ 回收是家务，不该有能力让审计失败。
            logger.debug(f"[OS-Audit] 截图回收跳过（不影响审计）: {e}")

    def prune_screenshots(self) -> dict:
        """按体积回收截图目录。返回这次删了什么（给测试和日志看）。"""
        d = self._screenshot_dir
        if d is None or not d.is_dir():
            return {"deleted": 0, "freed": 0, "kept": 0, "total": 0}

        # ⭐ 一次 `stat()` 同时拿到 size 和 mtime。
        # ⚠️ 文件可能在 glob 与 stat 之间被别人删掉/正在写 —— 跳过即可。
        items: list[tuple[pathlib.Path, int, float]] = []
        for f in d.glob("*.png"):
            try:
                st = f.stat()
            except OSError:
                continue
            items.append((f, st.st_size, st.st_mtime))
        if not items:
            return {"deleted": 0, "freed": 0, "kept": 0, "total": 0}

        budget = self._SHOT_BUDGET_BYTES
        outlier_at = int(budget * self._SHOT_OUTLIER_RATIO)
        deleted, freed, outliers = 0, 0, 0
        total = sum(sz for _f, sz, _m in items)

        # ⭐⭐ **没超预算 → 一张都不删。**
        #
        # 🔴 第一版把「它是异常」当成了独立的删除理由，于是**总量只有 150MB
        #    （预算 200MB）时也会把 5 张 30MB 的图全删光** —— 目录归零、证据全失，
        #    而磁盘上根本没有任何压力需要缓解。
        # 📌 ③ 的判据原话：「它**不但在吃预算**，它作为证据本身也是坏的」——
        #    **「吃预算」是触发条件，「是坏证据」只是「该删它而不是删别人」的排序理由。**
        #    后者当初被误当成了触发条件。
        # ⭐ **没有成本被付出，就没有回收的理由。** 这条对三类回收都成立
        #    （截图 / 命令输出 / 记忆水位），不是这里的特例。
        if total <= budget:
            return {"deleted": 0, "freed": 0, "kept": len(items),
                    "total": total, "outliers": 0}

        def _rm(p: pathlib.Path) -> bool:
            try:
                p.unlink()
                return True
            except OSError:
                return False

        # ③ 超预算了 → 异常优先删（**先标记、后删除**，中间过一次地板）
        #
        # ⚠️ 地板要作用在【最终状态】上，不管文件是被哪条规则删的 ——
        #    否则「所有图都变大了」（多屏拼接 / 换无压缩格式）这种情况下，
        #    每一张新图都越过门槛 → **目录永远是空的，而没有任何东西会说一声**。
        # 📌 用的就是 ② 那句「证据 > 磁盘」：**一张坏证据 > 零证据**，
        #    而且它的大小本身就是诊断信息。
        marked = [t for t in items if t[1] > outlier_at]
        survivors = [t for t in items if t[1] <= outlier_at]
        # ⚠️ 条件是「**一张正常图都没有**」，不是「正常图不足地板」。
        #    🔴 写成后者时那个例子会反过来：9 张 1MB 好证据 + 1 张 200MB 异常
        #       → 9 < 地板 10 → 于是**从异常里留一张补地板**，留的正是那张坏的。
        #    📌 有 9 张正常图在，那张 200MB 就是**可辨认的异常**（正常长什么样，
        #       这 9 张已经证明了）—— 拿 1 张已知是坏的换「掉到 9 张」，明显划算。
        #       只有一张正常的都没有时，才轮到「坏证据 > 零证据」。
        if not survivors and marked:
            marked.sort(key=lambda t: t[2])          # 旧 → 新
            need = self._SHOT_FLOOR
            spared, marked = marked[-need:], marked[:-need]
            survivors.extend(spared)
            logger.warning(
                f"[OS-Audit] ⚠️ 截图目录里有异常大图（> {outlier_at / 1024 / 1024:.0f} MB），"
                f"而正常大小的图不足 {self._SHOT_FLOOR} 张 —— 保留其中最新 "
                f"{len(spared)} 张以免证据归零。📌 这条反复出现说明**截图本身**"
                f"出了问题（或门槛该调了），不是回收出了问题。"
            )
        for f, size, _m in marked:
            if _rm(f):
                deleted += 1
                freed += size
                total -= size
                outliers += 1

        # ① 删最旧的，直到 ≤ 预算；② 但不低于地板
        survivors.sort(key=lambda t: t[2])          # 旧 → 新
        i = 0
        while total > budget and len(survivors) - i > self._SHOT_FLOOR:
            f, size, _m = survivors[i]
            if _rm(f):
                deleted += 1
                freed += size
                total -= size
            i += 1

        kept = len(survivors) - i
        if deleted:
            logger.info(
                f"[OS-Audit] 截图回收：删 {deleted} 张（其中异常 {outliers} 张）、"
                f"释放 {freed / 1024 / 1024:.1f} MB，剩 {kept} 张 / "
                f"{total / 1024 / 1024:.1f} MB（预算 {budget / 1024 / 1024:.0f} MB，"
                f"地板 {self._SHOT_FLOOR} 张）"
            )
        return {"deleted": deleted, "freed": freed, "kept": kept,
                "total": total, "outliers": outliers}

    @staticmethod
    def _safe_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """对明显敏感的字段做脱敏。只读动作通常无敏感参数，但 type_text/
        clipboard 等后续动作可能含密码，这里预置脱敏逻辑。"""
        if not isinstance(params, dict):
            return {}
        out = {}
        _SENSITIVE = {"password", "passwd", "secret", "token", "api_key", "text"}
        for k, v in params.items():
            if k.lower() in _SENSITIVE and isinstance(v, str) and len(v) > 0:
                out[k] = f"<redacted:{len(v)}chars>"
            else:
                out[k] = v
        return out

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        """读最近 n 条审计记录（验收/调试用）。"""
        if not self._log_path.exists():
            return []
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            out = []
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
            return out
        except Exception as e:
            logger.error(f"[OS-Audit] 读取审计日志失败: {e}")
            return []


_audit: Optional[OSAuditLogger] = None
_audit_lock = threading.Lock()


def get_audit_logger() -> OSAuditLogger:
    global _audit
    if _audit is None:
        with _audit_lock:
            if _audit is None:
                _audit = OSAuditLogger()
    return _audit
