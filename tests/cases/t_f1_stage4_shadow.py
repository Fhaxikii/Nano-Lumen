# -*- coding: utf-8 -*-
"""Suspension → WaitCondition 三步迁移的永久终态验收。

文件名保留 `shadow` 是为了让历史测试入口与全量 runner 不断链；测试内容不再
维护观测期与切读期的脚手架。那段迁移证明过两件事：状态存储可以双跑后对事实，且 shadow
只能观察不能纠正。切写完成后，继续运行对答案反而是在拿权威跟自己比。

这里长期钉住最终有价值的性质：
  · 所有消费者得到完整 WaitRecord，不再把六态折成 active/resolved；
  · DUE_FOR_REVIEW 仍能被驱动重试，resolve 与 cancel 保留不同历史；
  · `get` 严格查新 ID，历史 `legacy_susp_id` 只由 `find_by_id` 单点兼容；
  · 老 runtime DB 会幂等补列且不丢行；
  · UI 对 CANCELLED / EXPIRED / ORPHANED / CONSUMED 的措辞来自真实权威状态。

用法：py -3.10 tests\cases\t_f1_stage4_shadow.py
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

from core.runtime.clock import FakeClock
from core.runtime.kernel import Command, reset_kernel_for_tests
from core.runtime.store import RuntimeStore
from core.runtime import task as _task
from core.runtime import waitcond as W

BASE_T = 1_700_000_000.0
_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def make_kernel(tmp: pathlib.Path, *, db_name: str = "rt.db"):
    _task.clear_blocker_providers_for_tests()
    tmp.mkdir(parents=True, exist_ok=True)
    return reset_kernel_for_tests(
        store=RuntimeStore(tmp / db_name), clock=FakeClock(BASE_T))


def t_terminal_api(tmp: pathlib.Path) -> None:
    print("\n[1] ⭐⭐⭐ 完整 WaitRecord / 单一权威")
    k = make_kernel(tmp / "api")
    first = W.open_wait(reason="等 CI", wake_on=[W.WakeSource.TIMER],
                        timer_seconds=100)
    second = W.open_wait(reason="等后台", wake_on=[W.WakeSource.BACKGROUND],
                         bg_ref="bg1")
    full = isinstance(first, W.WaitRecord) and isinstance(second, W.WaitRecord)
    check(full, "open_wait 直接返回完整 WaitRecord",
          f"{type(first).__name__}/{type(second).__name__}")
    if not full:
        return

    check(W.get(k, first.wait_id) == first, "get 严格按 wait_id 读取")
    check(W.find_by_id(k, first.wait_id).wait_id == first.wait_id,
          "find_by_id 可查当前 id")
    check([r.wait_id for r in W.list_live(k)] == [second.wait_id, first.wait_id],
          "list_live 默认新记录在前")
    check([r.wait_id for r in W.list_live(k, oldest_first=True)]
          == [first.wait_id, second.wait_id],
          "旧顺序由消费者显式声明 oldest_first")

    check(W.list_due_wakeups(k, BASE_T + 50) == [], "到点前捞不到")
    check([r.wait_id for r in W.list_due_wakeups(k, BASE_T + 200)]
          == [first.wait_id], "到点后返回完整记录")
    k.clock.advance(101)
    W.tick(k)
    check(W.get(k, first.wait_id).status == W.WaitStatus.DUE_FOR_REVIEW,
          "到点只进入 DUE_FOR_REVIEW，不冒充完成")
    check([r.wait_id for r in W.list_due_wakeups(k)] == [first.wait_id],
          "DUE_FOR_REVIEW 仍可被驱动重试")

    check(W.resolve_wait(first.wait_id, "timer") is True, "正常收尾成功")
    check(W.get(k, first.wait_id).status == W.WaitStatus.CONSUMED,
          "resolve 保持 SATISFY → CONSUME 两步")
    check(W.resolve_wait(first.wait_id, "timer") is False, "重复 resolve 幂等")
    check(W.cancel_wait(second.wait_id, "model-cancel") is True, "主动取消成功")
    check(W.get(k, second.wait_id).status == W.WaitStatus.CANCELLED,
          "cancel 保留 CANCELLED，而非伪装成正常完成")
    check(W.list_live(k) == [], "两条收尾后活列表为空")


def t_historical_id_durable(tmp: pathlib.Path) -> None:
    print("\n[2] ⭐⭐ 历史 legacy_susp_id 只作反查，且跨重启")
    db = tmp / "historical" / "rt.db"
    k = make_kernel(tmp / "historical")
    with k.store.write_txn() as conn:
        conn.execute(
            "INSERT INTO wait_conditions "
            "(wait_id,kind,status,wake_on,reason,legacy_susp_id,orphan_at,"
            "revision,created_at,updated_at) VALUES "
            "('w_historical','timer','WAITING','[\"timer\"]','旧等待',"
            "'susp_historical',?,1,?,?)",
            (BASE_T + 1800, BASE_T, BASE_T),
        )
    check(W.get(k, "susp_historical") is None,
          "get 不把历史 id 混进当前 id 查询")
    rec = W.find_by_id(k, "susp_historical")
    check(rec is not None and rec.wait_id == "w_historical",
          "find_by_id 单点兼容历史 id")

    k2 = reset_kernel_for_tests(store=RuntimeStore(db), clock=FakeClock(BASE_T))
    rec2 = W.find_by_id(k2, "susp_historical")
    check(rec2 is not None and rec2.legacy_susp_id == "susp_historical",
          "历史引用跨重启仍可查")
    check(rec2.bg_ref is None and "susp_historical" not in rec2.reason,
          "历史 id 没有串进后台引用或人类可见原因")


def t_add_column_migration(tmp: pathlib.Path) -> None:
    print("\n[3] ⭐ 老 runtime DB 幂等补列且不丢数据")
    old_dir = tmp / "old"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_db = old_dir / "legacy.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript(
        "CREATE TABLE wait_conditions (wait_id TEXT PRIMARY KEY, kind TEXT,"
        " status TEXT, wake_on TEXT, reason TEXT, bg_ref TEXT, fire_at REAL,"
        " expire_at REAL, orphan_at REAL, result_json TEXT, satisfied_at REAL,"
        " satisfied_by TEXT, consumed_at REAL, resolution TEXT,"
        " owner_task_id TEXT, owner_turn_id TEXT, revision INTEGER,"
        " created_at REAL, updated_at REAL, closed_at REAL);"
        "INSERT INTO wait_conditions (wait_id,kind,status,wake_on,revision,"
        " created_at,updated_at) VALUES "
        "('w_old','timer','WAITING','[\"timer\"]',1,1.0,1.0);"
        "PRAGMA user_version=4;"
    )
    conn.commit()
    conn.close()

    RuntimeStore(old_db)
    conn2 = sqlite3.connect(str(old_db))
    cols = [row[1] for row in conn2.execute("PRAGMA table_info(wait_conditions)")]
    row = conn2.execute(
        "SELECT wait_id, legacy_susp_id FROM wait_conditions WHERE wait_id='w_old'"
    ).fetchone()
    version = conn2.execute("PRAGMA user_version").fetchone()[0]
    conn2.close()
    check("legacy_susp_id" in cols and "intent" in cols,
          "老库补上历史反查列与等待意图列")
    check(row == ("w_old", None), "已有行保留，未知历史 id 如实为 NULL", str(row))
    from core.runtime.store import _SCHEMA_VERSION
    check(version == _SCHEMA_VERSION, f"schema 版本升到 v{_SCHEMA_VERSION}", str(version))
    RuntimeStore(old_db)
    check(True, "重复启动迁移幂等")


def _open_raw(k, wait_id: str, *, expire_at=None, orphan_at=None) -> None:
    payload = {
        "wait_id": wait_id,
        "kind": W.WaitKind.TIMER,
        "wake_on": [W.WakeSource.TIMER],
        "reason": wait_id,
        "fire_at": BASE_T + 3600,
    }
    if expire_at is not None:
        payload["expire_at"] = expire_at
    if orphan_at is not None:
        payload["orphan_at"] = orphan_at
    k.submit(Command(kind=W.OPEN, payload=payload))


def t_ui_words_from_full_status(tmp: pathlib.Path) -> None:
    print("\n[4] ⭐⭐⭐ UI 措辞读取真实六态（永久回归项）")
    # 只调用纯映射方法，不构造 NiceGUI 组件；数据库与状态转换全部走真实实现。
    from app import WebUI

    cases: list[tuple[str, str, str]] = []

    k = make_kernel(tmp / "ui-cancel")
    cancelled = W.open_wait(reason="取消", wake_on=[W.WakeSource.TIMER], timer_seconds=10)
    W.cancel_wait(cancelled.wait_id, "model-cancel")
    cases.append((cancelled.wait_id, "✕ 已取消等待", "CANCELLED"))

    k = make_kernel(tmp / "ui-expire")
    _open_raw(k, "w_expired", expire_at=BASE_T)
    W.tick(k)
    cases.append(("w_expired", "✕ 没等到（已过期）", "EXPIRED"))

    k = make_kernel(tmp / "ui-orphan")
    _open_raw(k, "w_orphaned", orphan_at=BASE_T)
    W.tick(k)
    cases.append(("w_orphaned", "✕ 等的东西没回来", "ORPHANED"))

    k = make_kernel(tmp / "ui-done")
    done = W.open_wait(reason="完成", wake_on=[W.WakeSource.TIMER], timer_seconds=10)
    W.resolve_wait(done.wait_id, "timer")
    cases.append((done.wait_id, "▶ 到点了，继续", "CONSUMED"))

    # 每个 case 的 kernel 必须在调用时是对应那一个；上面连续 reset 后只有最后一个全局有效，
    # 因此逐项重开数据库来验证真实持久状态。
    db_dirs = ("ui-cancel", "ui-expire", "ui-orphan", "ui-done")
    for (wait_id, expected, status), db_dir in zip(cases, db_dirs):
        reset_kernel_for_tests(
            store=RuntimeStore(tmp / db_dir / "rt.db"), clock=FakeClock(BASE_T))
        text, _ = WebUI._pill_settle_words(
            object(), wait_id, "▶ 继续", "#059669")
        check(text == expected, f"{status} 使用权威措辞", text)


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        tmp = pathlib.Path(directory)
        t_terminal_api(tmp)
        t_historical_id_durable(tmp)
        t_add_column_migration(tmp)
        t_ui_words_from_full_status(tmp)

    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{total} 通过" + ("" if passed == total else " —— 失败项："))
    for ok, name, note in _results:
        if not ok:
            print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
