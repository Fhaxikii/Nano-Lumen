# -*- coding: utf-8 -*-
"""运行作用域与事实生命周期。

═══ 一句话 ═══

**持久记忆能证明"过去发生过什么"，证明不了"现在仍然成立"。**

而本条最有牙齿的那一格（③）是被实测逼出来的（2026-08-13）：

    07:55:39  [TOKEN-PLAN] core_tools=6（不含 create_new_skill，本轮也没 load_tools）
    07:55:49  [Router] create_new_skill 触发（requirement='部署这个吧'）  ← 照样执行了

用户对着一张待审卡说「部署这个吧」，模型**凭上一轮的 schema 记忆**调了
`create_new_skill`，而 `answer_open_interaction` 就在它手边。
📌 **要注入一句话，先让它成为真的** —— ④ 那句 "Only tools attached to this model
   request are callable" 在 ③ 落地之前是**假话**。

用法：
  py -3.10 tests\cases\t_f6_runtime_scope.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401  GBK 控制台保护
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


ORCH = module_text("core.orchestrator")
IDENT = module_text("core.runtime.identity")
HEALTH = module_text("core.health")
OTREE = ast.parse(ORCH)


def _strip(node, src: str) -> str:
    """函数源码，**注释【和 docstring】都剥掉**。

    ⚠️⚠️ **docstring 必须一起剥，这是本项目第五次踩同一个坑，而且形态又变了。**
    今天上午刚在 `t_d13_explorer_scope._prompt_text` 里修过一次（那次是"取样模型
    会读到什么"漏了 docstring），下午写这个新 helper 时**又只剥了 `#`** ——
    于是「`_execution_scope_block` 不列工具名」这条断言，被它自己 docstring 里
    那句"`load_tools` 之后 `tools_manifest` 会在 turn 中途变化"打红了。

    📌 **判据：任何「这段代码里有没有 X」的检查，必须同时排除注释【和 docstring】。**
       两者都是写给人看的，区别只是一个进 AST、一个不进 —— 而"进不进 AST"
       是实现细节，不是判据。
    📌 **而这次的教训更贵：在一个 helper 里修好了它，转头在另一个 helper 里
       原样重建了同一个坑。** 修一处不等于修掉这个形状。
    """
    seg = ast.get_source_segment(src, node) or ""
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        doc = ast.get_source_segment(src, body[0]) or ""
        if doc:
            seg = seg.replace(doc, "", 1)
    return "\n".join(l for l in seg.splitlines() if not l.strip().startswith("#"))


def _fn_code(tree, src, name, owner=None):
    """按名字取函数源码（注释与 docstring 均已剥掉）。见 `_strip`。"""
    for n in ast.walk(tree):
        if owner is not None:
            if not (isinstance(n, ast.ClassDef) and n.name == owner):
                continue
            for s in n.body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == name:
                    return _strip(s, src)
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return _strip(n, src)
    return ""


# ══════════════════════════════════════════════════════════════════════════
# ① runtime_id
# ══════════════════════════════════════════════════════════════════════════

def t_runtime_id() -> None:
    print("\n[1] ① runtime_id —— 当前值内存权威，历史写库")
    from core.runtime import identity as I

    a, b = I._new_runtime_id(), I._new_runtime_id()
    check(a != b, "⭐ 每次生成都不同（同一秒内起两个进程也必须是两个身份）", f"{a} / {b}")
    check(a.startswith("rt_"), "有可读前缀", a)
    check(I.current_runtime_id() == I.current_runtime_id(),
          "⭐ 进程内恒定（懒生成一次，之后不变）")

    # 🔴 这条是整组最要紧的一条
    code = _fn_code(ast.parse(IDENT), IDENT, "current_runtime_id")
    check("kernel" not in code and "store" not in code and "SELECT" not in code,
          "⭐⭐⭐ **`current_runtime_id()` 不碰数据库** —— "
          "🔴 从库里捞『最后一条』当 current，等于把一个历史身份冒充成当前身份。"
          "📌 库里那些全是**历史**；真正的当前身份只能来自本进程刚生成的那个。")

    # ⚠️ 刻意没有 previous_shutdown_clean：现在造不出这个值
    store = module_text("core.runtime.store")
    _ddl_i = store.find("CREATE TABLE IF NOT EXISTS runtime_runs")
    _ddl = store[_ddl_i:_ddl_i + 400] if _ddl_i > 0 else ""
    check(bool(_ddl), "runtime_runs 建表语句存在")
    check("previous_shutdown_clean" not in _ddl,
          "⚠️ **没有 `previous_shutdown_clean` 字段** —— 全项目零 clean-shutdown 标记，"
          "而「启动时没发现 interrupted task」推不出「上次干净退出」。"
          "📌 不要用「没看到尸体」推导「寿终正寝」。")

    # 反向：真的能写能读回来
    import tempfile
    from core.runtime.store import RuntimeStore
    from core.runtime.kernel import RuntimeKernel
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        k = RuntimeKernel(RuntimeStore(pathlib.Path(d) / "t.db"))
        I.record_run(k, "恢复了 3 个 Task")
        rows = k.store.connect().execute(
            "SELECT runtime_id, recovery_summary FROM runtime_runs").fetchall()
        check(len(rows) == 1 and rows[0]["recovery_summary"] == "恢复了 3 个 Task",
              "⚠️ 反向：确实写进去了，且带着恢复摘要（不是空跑）", str(len(rows)))
        check(I.previous_run(k) is None,
              "⭐ `previous_run` **不会把本次当成上一次**（库里只有自己时返回 None）")
        k.store.close_thread_conn()


# ══════════════════════════════════════════════════════════════════════════
# ② Restart Awareness
# ══════════════════════════════════════════════════════════════════════════

def t_restart_notice() -> None:
    print("\n[2] ⭐⭐ ② 重启提示 —— 只复用注入位置，不复用 RecentSystemEvents 语义")

    # 🔴🔴 这一条是复查时被 外部评审 抓出来的：原方案打算直接塞进 RecentSystemEvents
    check("Use them only if the user asks what just happened" in HEALTH,
          "⚠️ 前置：`RecentSystemEvents` 的表头确实写着"
          "「除非用户问起否则别用」（这条断言的全部依据）")
    check("get_system_events" not in IDENT,
          "⭐⭐⭐ **重启提示【不】走 `RecentSystemEvents`** —— "
          "🔴 它的表头对模型说「除非被问否则忽略我」，而重启提示恰恰是"
          "**没问也必须影响判断**。塞进去等于给最不该忽略的消息挂上一句忽略我。"
          "📌 **复用一个通道时要连它的措辞一起复用 —— 那句措辞可能正好否定你要传达的东西。**")

    arm = _fn_code(ast.parse(IDENT), IDENT, "arm_restart_notice")
    check("if not prev:" in arm and "return" in arm,
          "⭐ **只在真有上一次运行时才挂** —— 首次运行不该说「重启了」")
    check("load_tools" in arm and "authorization" in arm.lower(),
          "提示内容点名了会失效的那几类运行态（工具 schema / 临时授权 / 句柄…）")
    check("same continuous Nano" in arm and "amnesiac" in arm,
          "⭐ 同时明说「你还是同一个 Nano，记忆是连续的」—— 不许表现得像失忆")

    # 消费判定：provider_error 不算
    core = _fn_code(OTREE, ORCH, "_stream_decision_core", owner="Orchestrator")
    check("_model_really_spoke" in core,
          "⭐ 有「模型是否真的产出过」这个判定")
    check('"provider_error"' in core and "consume_restart_notice" in core,
          "⭐⭐⭐ **`provider_error` 不算已消费** —— "
          "🔴 它是 `done` 事件里的一个 `decision_type`，不是独立事件类型；"
          "只看 `done` 会把「请求根本没成功」当成「模型已被告知」，"
          "于是**这次重启永远不会被说出来**")


# ══════════════════════════════════════════════════════════════════════════
# ③ request tool contract 闸门（本条唯一有牙齿的一格）
# ══════════════════════════════════════════════════════════════════════════

def t_request_contract_gate() -> None:
    print("\n[3] ⭐⭐⭐ ③ 本次 request 的 tool contract 成为执行资格")
    import core.orchestrator as O

    check(hasattr(O.Orchestrator._ToolFailCause, "TOOL_NOT_ACTIVE"),
          "⭐ 新增 cause `TOOL_NOT_ACTIVE`")
    _c = O.Orchestrator._ToolFailCause
    check(len({_c.UNKNOWN_TOOL, _c.DISABLED_TOOL, _c.BAD_PARAMS,
               _c.TOOL_ERROR, _c.TOOL_NOT_ACTIVE}) == 5,
          "⚠️ **它是第五类，不是复用前四类** —— "
          "📌 塞进 UNKNOWN_TOOL 是撒谎（它明明存在）、塞进 DISABLED_TOOL 也是"
          "（它没被禁用）、塞进 TOOL_ERROR 更错（它压根没跑）。"
          "失败信息必须正确，而且正确的下一步各不相同")

    gate = _fn_code(OTREE, ORCH, "_execute_one_tool_call", owner="Orchestrator")

    # 🔴🔴 判据来源：必须是 request 快照，不能重算
    check("_pending_loaded_manifests" not in gate,
          "⭐⭐⭐ **闸门不碰 `_pending_loaded_manifests`** —— "
          "🔴 它是**中转缓冲区**：本批执行完就 append 进 `tools_manifest` 然后立刻清空。"
          "拿它当判据会**把刚刚合法 load 成功的工具拦掉**（方向刚好反了）")
    check("active_tool_names" in gate,
          "⭐ 判据是随这次 decision 传下来的 request 快照")

    core = _fn_code(OTREE, ORCH, "_stream_decision_core", owner="Orchestrator")
    check("frozenset" in core and "tools_manifest" in core,
          "⭐⭐ 快照取自**发请求那一刻**的 `tools_manifest`（就是发给 provider 的那份）")
    check("decision.active_tool_names" in core,
          "⚠️ 挂在 `decision` 上而不是 `self` —— "
          "📌 一个「属于某次请求」的事实，就该挂在那次请求的产物上"
          "（`_stream_decision_core` 当初改用局部 state 正是为了避免实例属性互相覆盖）")

    # 五格顺序：health 在前
    _i_health = gate.find("tool_block_reason")
    _i_gate = gate.find("TOOL_NOT_ACTIVE")
    check(0 < _i_health < _i_gate,
          "⭐⭐ **health 闸在前** —— 已下架的工具要出 Health 的"
          "「currently unavailable + recovery hint」，不能被本闸抢成 TOOL_NOT_ACTIVE",
          f"health@{_i_health} < gate@{_i_gate}")
    check("Preload.DEFERRED" in gate,
          "⭐ 只拦 DEFERRED —— **CORE 与 HIDDEN 都不进这道闸**。"
          "📌 HIDDEN（如 WriteSkill）绝不能被建议去 `load_tools`："
          "它的定义就是「处理得了但刻意不给正式通路」，而 load_tools 本来就搜不到它")
    check("is_eligible" in gate,
          "⭐ `availability=False` 的照旧放行到 handler —— "
          "由它说「你要处理的那个对象已经没了」，而不是任何一种「不存在」"
          "（`eligible ⊆ resolvable` 单向包含正为它设计）")
    check("load_tools" in gate,
          "⭐ 诊断里给出正确的下一步（load_tools 然后下一步再调）")

    # ⚠️ fail-open 是刻意的
    check("if _active:" in gate,
          "⚠️ **拿不到快照时不校验（fail-open）** —— "
          "📌 一道新加的闸，在信息不全时应当放行，否则会在自己还没接全的地方制造假故障")


# ══════════════════════════════════════════════════════════════════════════
# ④ [Execution Scope]
# ══════════════════════════════════════════════════════════════════════════

def t_execution_scope() -> None:
    print("\n[4] ④ 每轮 [Execution Scope] 动态段")
    import core.orchestrator as O
    blk = O.Orchestrator._execution_scope_block(O.Orchestrator)  # 不需要实例状态

    check("[Execution Scope]" in blk and "runtime_id=" in blk, "段落成形且带 runtime_id")
    check("this model request" in blk,
          "⭐⭐⭐ **措辞是 `this model request`** —— "
          "🔴 不能写 `this turn`：一个用户 turn 内部有**多次** ReAct model request，"
          "而 `load_tools` 之后 schema 会在 turn 中途变化。"
          "📌 这次要明确的粒度恰恰就是 request，写 turn 就把粒度说错一档")
    check("turn" not in blk.lower().replace("returns", ""),
          "⚠️ 全段不出现 turn 这个词", blk[:0])
    check("load_tools" in blk, "指出了正确出路（不是只禁止）")

    # ⚠️ 不许列工具名 —— 那会变成第二份名单
    _code = _fn_code(OTREE, ORCH, "_execution_scope_block", owner="Orchestrator")
    check("tools_manifest" not in _code and "advertised" not in _code,
          "⭐⭐ **不列工具列表** —— 当前 API manifest 已经是工具的唯一权威；"
          "在这里再列一遍就是第二份名单（正是 [F4] 花一整轮消灭的东西）。"
          "📌 也正因为它只讲规则不讲内容，它在一个 turn 内是恒定的")
    check("skill_exploration" not in blk,
          "⚠️ scope 里没有 `skill_exploration` —— 探索子循环已于 2026-08-13 拆除")


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 与的关系：这是一次【明示的推翻】，不是偷偷改
# ══════════════════════════════════════════════════════════════════════════

def t_supersede_declared() -> None:
    print("\n[5] ⚠️ 推翻工具目录那条既有规定，必须留声明")
    cat = module_text("core.tools.catalog")
    check("没附带但仍可执行" in cat,
          "⚠️ 前置：那条原文确实还在（`deferred 工具 schema 没附带但仍可执行是正常状态`）")
    # ⚠️ 区段锚点用的是**那一段自己的标题行**，不是代号 —— 代号会随清理消失。
    gate = ORCH[ORCH.find("本次 request 的 tool contract 闸门"):][:3000]
    check("推翻" in gate and "catalog.py" in gate,
          "⭐⭐ 闸门处写明了**它推翻的是工具目录里的哪一条** —— "
          "📌 读 `catalog.py` 的人会看到那句「仍可执行是正常状态」；"
          "不写这条声明，会以为 Catalog 被改坏了")


def main() -> int:
    t_runtime_id()
    t_restart_notice()
    t_request_contract_gate()
    t_execution_scope()
    t_supersede_declared()
    passed = sum(1 for ok, _, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    if passed == total:
        print(f"结果：{passed}/{total} 通过")
    else:
        print(f"结果：{passed}/{total} 通过 —— 失败项：")
        for ok, name, note in _results:
            if not ok:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
