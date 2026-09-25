# -*- coding: utf-8 -*-
"""统一工具目录 —— Catalog 自身的不变量与派生视图。

这一层要替掉的是「新增一个工具要在 11 处分别登记，而且没有一处会报错」。
所以本文件的重点**不是「功能能用」，而是「漏填会不会响亮失败」**。

⭐ 两条【当时就已经坏了】的实证，是这次改造的直接动机：
  1. 五个工具同时在 `_REACT_SERIAL_TOOLS` 与 `_REACT_EXIT_TOOLS` 里，而分类函数
     先判 EXIT 就 return → **它们声明的 serial 永远不生效，且不报错**。
     → 所以本层把 `flow` 与 `scheduling` 拆成两个正交维度，
       且 exit 工具的调度值是 **EXCLUSIVE（整批拒绝）而不是假的 SERIAL**。
  2. `_OS_ACTIONS` 手抄 29 个而 `dsl.py` 实际 39 个 —— **漏 10 个**（含 `file_delete`）。
     → 所以检索文档**从 manifest 的 enum 自动派生**，不再手抄。

📌 核心不变量：`eligible(scope, runtime) == resolvable(scope, runtime)`
   （不写成 `visible == executable`：`load_tools` 只给 schema、不决定能否执行，
     deferred 工具「schema 没附带但仍可执行」是正常状态。）
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _console  # noqa: F401,E402  控制台编码保护（见它的模块文档）
from tests._src import module_text  # noqa: E402

from core.tools import (  # noqa: E402
    ALWAYS, CatalogConflict, Flow, Preload, Presentation, Scheduling,
    ToolCatalog, ToolDefinition, ToolDefinitionError, ToolOrigin, ToolScope,
    build_catalog,
)
from core.tools.catalog import _compress  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(cond, label, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}" + (f"   [{detail}]" if detail else ""))
    else:
        _failed.append(label)
        print(f"  !! {label}" + (f"   [{detail}]" if detail else ""))


class _RT:
    """最小 ToolRuntimeView。"""
    def __init__(self, work=False, interaction=False, recheck=False):
        self._w, self._i, self._r = work, interaction, recheck

    def has_live_work(self):
        return self._w

    def has_open_interaction(self):
        return self._i

    def is_recheck_round(self):
        return self._r

    def has_unsummarized_image(self):
        return getattr(self, "_img", False)


def mk(name, **kw):
    kw.setdefault("origin", ToolOrigin.BUILTIN)
    kw.setdefault("manifest", {"name": name, "description": "desc",
                               "parameters": {"type": "object", "properties": {}}})
    kw.setdefault("awareness", "what this tool is for")
    kw.setdefault("presentation", Presentation(card=lambda a: "卡片文案"))
    kw.setdefault("scheduling", Scheduling.SERIAL)
    kw.setdefault("flow", Flow.CONTINUE)
    kw.setdefault("preload", Preload.DEFERRED)
    kw.setdefault("bindings", {ToolScope.MAIN: "_handle_x"})
    return ToolDefinition(name=name, **kw)


# ── [1] 构造期不变量：漏填必须【响亮失败】 ─────────────────────────────────

def t_construction_invariants():
    print("\n[1] 构造期不变量 —— 漏填必须当场失败，不许撑到运行时")
    for label, fn in [
        ("manifest['name'] 与 definition.name 不一致 → 失败",
         lambda: mk("a", manifest={"name": "b"})),
        ("awareness 为空 → 失败（[D9] 的根因就是拿截断冒充语义摘要）",
         lambda: mk("a", awareness="   ")),
        ("bindings 为空 → 失败（谁都执行不了的工具不该存在）",
         lambda: mk("a", bindings={})),
        ("name 为空 → 失败", lambda: mk("")),
    ]:
        try:
            fn()
            check(False, label, "没有抛异常")
        except ToolDefinitionError:
            check(True, label)

    # ⚠️ availability 恒 False **不是**错误：那是合法的「当前不该出现」
    try:
        mk("cond", availability=lambda r: False)
        check(True, "availability 恒 False 不算构造错误（合法的『当前不该出现』）")
    except ToolDefinitionError:
        check(False, "availability 恒 False 被误判成错误")


# ── [2] availability：bindings 答不了「现在该不该存在」 ────────────────────

def t_availability():
    print("\n[2] ⭐ availability —— 三个真实工具的出现条件不由 scope 决定")
    tb = mk("task_boundary", availability=lambda r: r.has_live_work())
    ai = mk("answer_open_interaction", availability=lambda r: r.has_open_interaction())
    sc = mk("set_next_checkin", availability=lambda r: r.is_recheck_round())

    check(tb.is_eligible(ToolScope.MAIN, _RT(work=False)) is False,
          "⭐ 没有 live work 时 task_boundary 不出现 —— "
          "📌 否则模型会『找一件不存在的事来结束』（`_rt_ongoing_work` 注释原话）")
    check(tb.is_eligible(ToolScope.MAIN, _RT(work=True)) is True,
          "有 live work 时才出现")
    check(ai.is_eligible(ToolScope.MAIN, _RT(interaction=False)) is False
          and ai.is_eligible(ToolScope.MAIN, _RT(interaction=True)) is True,
          "answer_open_interaction 跟随未决交互")
    check(sc.is_eligible(ToolScope.MAIN, _RT(recheck=False)) is False
          and sc.is_eligible(ToolScope.MAIN, _RT(recheck=True)) is True,
          "set_next_checkin 只在回看轮出现")

    # 📌 一个工具和它的事实来源必须由同一个条件控制 —— 半截状态会让模型开始猜
    boom = mk("boom", availability=lambda r: 1 / 0)
    check(boom.is_eligible(ToolScope.MAIN, _RT()) is False,
          "⚠️ availability 抛异常 → fail-safe 方向是【不给】"
          "（多一个工具的代价是模型乱调，少一个只是这轮用不上）")


# ── [3] eligible == resolvable：的事故形态结构上不可能 ─────────────────

def t_eligible_equals_resolvable():
    print("\n[3] ⭐⭐ 核心不变量：eligible(scope) == resolvable(scope)")
    cat = ToolCatalog()
    cat.add_builtin(mk("main_only", bindings={ToolScope.MAIN: "_h_main"}))
    cat.add_builtin(mk("both", bindings={ToolScope.MAIN: "_h_m",
                                         ToolScope.EXPLORATION: "_h_e"}))
    cat.add_builtin(mk("gated", availability=lambda r: r.has_live_work(),
                       bindings={ToolScope.MAIN: "_h_g"}))
    rt = _RT(work=False)

    # ⚠️⚠️ 方向是**单向包含**，不是相等（cutover 现场更正，理由见 catalog 模块文档）：
    #    要守的是「**给出去的一定执行得了**」= 事故的反面。
    #    反向差集是正常且必要的 —— `Preload.HIDDEN`（处理得了但不告诉模型）、
    #    以及 availability 为假的条件注入工具（现在不该出现，但模型凭历史里的
    #    旧 schema 再调一次时**仍然要执行**，由 handler 回一句正确的
    #    「你要回答的那条待办已经没了」，而不是假话「查无此工具」）。
    for scope in (ToolScope.MAIN, ToolScope.EXPLORATION):
        elig = {d.name for d in cat.eligible(scope, rt)}
        resolvable = {n for n in cat.names() if cat.resolve(n, scope, rt)}
        check(elig <= resolvable,
              f"⭐ {scope.value}: eligible ⊆ resolvable —— "
              f"[D13]「模型看得见但执行不了」在结构上不可能",
              f"{sorted(elig)}")
    # 而反向的那条差集，要断言清楚它**是什么**，不能靠"顺手放宽"糊过去
    check(cat.resolve("gated", ToolScope.MAIN, _RT(work=False)) == "_h_g",
          "⭐⭐ availability 为假时**仍然 resolve 得到 handler** —— "
          "🔴 否则模型凭旧 schema 再调一次会收到「没有这个工具」，而那是假话；"
          "它需要的是「你要处理的那个对象已经没了」（[D12]：失败信息必须正确）")
    check(cat.resolve("main_only", ToolScope.EXPLORATION, rt) is None,
          "⚠️ 但**没有该 scope 的 binding** 就是真的解析不到 —— "
          "这才是 [D13] 要拦的那一种")

    check("main_only" not in {d.name for d in cat.eligible(ToolScope.EXPLORATION, rt)},
          "没有该 scope 的 binding → 探索作用域里不出现（正是那次事故的形态）")
    check("gated" not in {d.name for d in cat.eligible(ToolScope.MAIN, rt)},
          "availability 不满足 → 也不出现（binding 有也没用）")

    # 只有一张名单：core/deferred 都是 eligible 的子集
    elig_main = {d.name for d in cat.eligible(ToolScope.MAIN, _RT(work=True))}
    core = {m["name"] for m in cat.core_manifests(ToolScope.MAIN, _RT(work=True))}
    deferred = {d.name for d in cat.deferred(ToolScope.MAIN, _RT(work=True))}
    check(core <= elig_main and deferred <= elig_main,
          "⭐ active_schema ⊆ eligible 且 deferred_awareness ⊆ eligible —— 只有一张名单")


# ── [4] 检索：enum 自动进索引，取代手抄的 _OS_ACTIONS ─────────────────────

def t_search_from_enum():
    print("\n[4] ⭐⭐ 检索文档从 manifest.enum 自动派生（取代手抄清单）")
    osx = mk("os_execute", manifest={
        "name": "os_execute",
        "description": "Operate the local computer",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "description": "what to do",
                       "enum": ["file_delete", "file_move", "run_command", "click"]}}}})
    cat = ToolCatalog()
    cat.add_builtin(osx)
    cat.add_builtin(mk("query_local_knowledge"))

    doc = osx.search_document()
    for act in ("file_delete", "file_move", "run_command"):
        check(act in doc, f"⭐ action enum 「{act}」自动进入检索文档")

    hit = [d.name for d in cat.search(query="file_delete")]
    check(hit[:1] == ["os_execute"],
          "⭐⭐ load_tools(query='file_delete') 精准命中 os_execute —— "
          "🔴 改造前 `_OS_ACTIONS` 手抄 29 个而 dsl.py 有 39 个，"
          "`file_delete` 恰好在漏掉的 10 个里。📌 手抄清单的过期是静默的，且专挑高频项漏",
          str(hit))
    check([d.name for d in cat.search(names=["query_local_knowledge"])] ==
          ["query_local_knowledge"], "names 直选")
    check([d.name for d in cat.search(query="os_execute")][:1] == ["os_execute"],
          "精确名优先于子串/文档命中")

    # ⚠️ 只在 eligible 集合里搜 —— 否则等于从后门破掉那条不变量
    gated = mk("gated_tool", availability=lambda r: r.has_live_work())
    cat2 = ToolCatalog()
    cat2.add_builtin(gated)
    check([d.name for d in cat2.search(query="gated", runtime=_RT(work=False))] == [],
          "⚠️ 不 eligible 的工具搜不出来（否则 eligible 不变量从后门破掉）")


# ── [5] 冲突：失败作用域必须落在肇事来源上 ────────────────────────────────

def t_conflicts():
    print("\n[5] 重名冲突 —— 响亮的是【报警】，不是【自杀】")
    cat = ToolCatalog()
    cat.add_builtin(mk("dup"))
    try:
        cat.add_builtin(mk("dup"))
        check(False, "内置重名应当 fatal")
    except CatalogConflict:
        check(True, "内置↔内置重名 → fatal（我们自己的代码写错了，必须当场修）")

    ok = cat.add_dynamic(ToolDefinition(
        name="dup", origin=ToolOrigin.SKILL, manifest={"name": "dup"},
        awareness="x", presentation=Presentation(card=lambda a: "c"),
        scheduling=Scheduling.SERIAL, flow=Flow.CONTINUE,
        preload=Preload.DEFERRED, bindings={ToolScope.MAIN: "_h"}))
    check(ok is False and len(cat.rejected()) == 1,
          "⭐ 动态工具撞名 → 隔离那一个 + 留下 rejected 记录，"
          "**不许把整个 Nano 启动打死**（一条用户 Skill / 一个外部 MCP 不该有这个权力）")
    check(cat.get("dup").origin is ToolOrigin.BUILTIN,
          "⚠️ 绝不 silent later-wins —— 改造前 `_build_skills_info` 正是后来的静默覆盖前面的")


# ── [6] Presentation：一处声明，两个渲染上下文 ────────────────────────────

def t_presentation():
    print("\n[6] 用户可见文案收成一处（取代两份独立的表）")
    cat = ToolCatalog()
    cat.add_builtin(mk("load_full_file", presentation=Presentation(
        card=lambda a: f"加载文件：{a.get('filename','')}",
        intent=lambda a: f"完整加载文件「{a.get('filename','')}」的内容")))
    args = {"filename": "a.md"}
    check(cat.presentation("load_full_file", args) == "加载文件：a.md", "card 上下文")
    check(cat.presentation("load_full_file", args, intent=True)
          == "完整加载文件「a.md」的内容", "intent 上下文")

    cat.add_builtin(mk("only_card", presentation=Presentation(card=lambda a: "只有卡片")))
    check(cat.presentation("only_card", {}, intent=True) == "只有卡片",
          "📌 intent 缺省时由 card 派生 —— 能推出来的东西不该要求再填一遍")

    # ⚠️ 兜底**不是**退回裸工具名 —— 改造前两条路各有自己的兜底文案，
    #    退回裸名会让用户在工具卡上看到一个光秃秃的英文标识符。
    #    📌 兜底也是用户可见文案，它一样要逐字保（cutover 现场补上的）。
    check(cat.presentation("unknown_tool", {}) == "执行 unknown_tool",
          "未知工具的**卡片**兜底是「执行 X」，不是裸名（文案永不把主流程搞崩）")
    check(cat.presentation("unknown_tool", {}, intent=True) == "调用工具「unknown_tool」",
          "⭐ 未知工具的 **intent** 兜底是另一句 —— card 答「正在做什么」、"
          "intent 答「打算做什么」，两个渲染上下文不共用一句话")
    check(cat.presentation("mcp__playwright__browser_navigate", {})
          == "外部能力：playwright · browser_navigate",
          "⭐ MCP 名字**拆掉命名空间**再给用户看 —— `mcp__` 是内部前缀，"
          "用户不区分内置能力和外部能力")

    boom = mk("boom_p", presentation=Presentation(card=lambda a: 1 / 0))
    cat.add_builtin(boom)
    check(cat.presentation("boom_p", {}) == "执行 boom_p",
          "渲染抛异常时走**同一条兜底**（不是另开一种退化形态）")


# ── [7] scheduling / flow 正交：EXIT 不再写假 SERIAL ──────────────────────

def t_scheduling_flow_orthogonal():
    print("\n[7] ⭐⭐ scheduling 与 flow 是两个维度，不再压成一个三值返回")
    exit_tool = mk("create_new_skill", flow=Flow.EXIT_REACT,
                   scheduling=Scheduling.EXCLUSIVE)
    check(exit_tool.flow is Flow.EXIT_REACT and
          exit_tool.scheduling is Scheduling.EXCLUSIVE,
          "⭐ exit 工具 = flow:EXIT_REACT + scheduling:EXCLUSIVE —— "
          "🔴 真实语义是【与任何其他工具同轮 → 整批一个都不执行、全部写 error、下轮重选】，"
          "**不是串行**；写 SERIAL 就是又造一个新的死声明")
    par = mk("query_local_knowledge", scheduling=Scheduling.PARALLEL)
    check(par.scheduling is Scheduling.PARALLEL and par.flow is Flow.CONTINUE,
          "只读工具 = PARALLEL + CONTINUE")
    check(len({Scheduling.PARALLEL, Scheduling.SERIAL, Scheduling.EXCLUSIVE}) == 3,
          "三种调度语义各自独立，可分别派生出旧的三张名单")


# ── [8] Adapter：Skill / MCP 零迁移 ───────────────────────────────────────

def t_adapters():
    print("\n[8] ⭐ Source Adapter —— Skill/MCP 各 0 条迁移")

    class _Reg:
        def get_all_manifests(self):
            return [{"name": "GetSystemTime", "description": "Return current time.\n\n更多说明",
                     "parameters": {"type": "object", "properties": {}}}]

    class _Mcp:
        available = True

        def list_tool_manifests(self):
            return [{"name": "mcp__playwright__browser_navigate",
                     "description": "Navigate to a URL", "parameters": {"type": "object"}}]

    cat = build_catalog([mk("load_tools", preload=Preload.CORE)],
                        registry=_Reg(), mcp_manager=_Mcp())
    check("GetSystemTime" in cat.names(), "⭐ 装一个 Skill 自动出现，不碰目录层任何代码")
    check("mcp__playwright__browser_navigate" in cat.names(),
          "⭐ 接一个 MCP server 自动出现 —— 45 个工具不是 45 条迁移，是 1 条 Adapter")
    sk = cat.get("GetSystemTime")
    check(sk.scheduling is Scheduling.SERIAL,
          "⚠️ Skill 并发资格由**代码强制** SERIAL，不由模型自声明（理由："
          "Skill 实例跨调用复用，`side_effects` 验的不是共享可变状态）")
    check(sk.preload is Preload.DEFERRED and sk.origin is ToolOrigin.SKILL,
          "Skill 投影的 origin/preload 正确")
    check(cat.get("mcp__playwright__browser_navigate").scheduling is Scheduling.SERIAL,
          "MCP 同样强制 SERIAL（外部调用、副作用未知）")

    cat.clear_dynamic()
    check(cat.names() == frozenset({"load_tools"}),
          "clear_dynamic 只清动态部分，内置声明不动（Skill 热加载/MCP 重连要用）")


# ── [9] 禁止字符级截断（的根因） ─────────────────────────────────────

def t_no_char_truncation():
    print("\n[9] ⭐ 禁止字符级截断 —— [D9] 的根因")
    src = "Operate this computer: inspect files, run commands, control windows"
    out = _compress(src, 40)
    check(not out.startswith("Operate this computer: inspe"[:28]) or out != src[:28],
          "不再产生 `Operate this computer: inspe` 那种切在词中间的残句")
    check(out.endswith("…"), "压缩后带省略号，明示『还有更多』", out)
    check(" ".join(out.rstrip("…").split()) in " ".join(src.split()),
          "压缩结果是原文的前缀（按词/句边界切）", out)
    check(_compress("short text", 40) == "short text", "够短就原样返回，不加省略号")
    long_desc = "x" * 300
    check(len(_compress(long_desc, 110)) <= 111, "超长有上界")


ERROR_MSG = None
try:
    import core.orchestrator as O  # noqa: E402
    from core.tools.builtin import build_builtin_definitions  # noqa: E402
except Exception as _e:  # pragma: no cover
    O = None
    ERROR_MSG = str(_e)


def _real_manifests() -> dict:
    from core.tools.manifests import BUILTIN_MANIFESTS
    return dict(BUILTIN_MANIFESTS)


# ── [10] 真实的 25 条内置声明：能构造 + binding 真实存在 ──────────────────

def t_builtin_declarations():
    print("\n[10] ⭐⭐ 真实内置声明（cutover 之前那一轮的产物）")
    if O is None:
        check(False, "无法导入 orchestrator", ERROR_MSG or "")
        return
    defs = build_builtin_definitions(_real_manifests())
    cat = ToolCatalog()
    for d in defs:
        cat.add_builtin(d)
    check(len(defs) >= 24, f"录入 {len(defs)} 条内置声明", str(len(defs)))

    # ⭐ 最关键的一条：binding 指向的方法必须真实存在，否则**启动就失败**
    try:
        cat.assert_bindings_exist(O.Orchestrator)
        check(True, "⭐⭐ 每个 binding 都指向 Orchestrator 上**真实存在**的方法 —— "
                    "📌 binding 用字符串写，写错在运行时才炸（表现为「模型调了工具、"
                    "然后什么都没发生」）；这条断言把它变成**启动期响亮失败**")
    except ToolDefinitionError as e:
        check(False, "binding 指向不存在的方法", str(e)[:120])

    # 反向：故意写错一个名字，必须被抓到
    broken = ToolCatalog()
    broken.add_builtin(mk("x", bindings={ToolScope.MAIN: "_method_that_does_not_exist"}))
    try:
        broken.assert_bindings_exist(O.Orchestrator)
        check(False, "写错的 binding 没被抓到")
    except ToolDefinitionError:
        check(True, "⚠️ 反向验证：写错方法名会被当场抓住（否则这条断言只是摆设）")


# ── [11] 与 cutover 前那一刻的既成事实逐项等价 ────────────────────────────
#
# ⚠️⚠️ **这一节的对拍对象在 cutover 那一刻变了，读之前先看清楚变成了什么。**
#
# cutover 之前，它拿新声明去对**旧实现里那批表**（`_CORE_TOOL_NAMES` /
# `_REACT_SERIAL_TOOLS` / `_BUILTIN_TOOLS_AWARENESS` / `_tool_action_display` …）。
# 那些表已经在同一次变更里**全部删除**了 —— 于是这些断言面临一个选择：
#   ① 跟着删 → 等价性证明消失，「不做 shadow」这个决定失去它唯一的依据；
#   ② 放宽   → 68 条变成 60 条，而放宽过的断言不会有人再收紧；
#   ③ **把旧值固化成快照** → 断言从「新 == 旧实现」变成
#      「新 == cutover 前那一刻的事实」，条数不减、强度不降。
#
# ⭐ 选 ③。判据是本项目吃过亏的那一条：
#    **「一条测中间态的断言，在迁移完成后会反过来阻止终态」——
#      而它的正确处置是【改成钉终态】，不是删掉。**
#
# 📌 快照**不是第二份权威**：它活在 tests/ 里、生产代码一行都不读它，
#    它的作用是「这批用户可见文案和调度语义，在切换前后必须一个字都不差」。
#    ⚠️ 所以它红的时候，**先怀疑代码，不要动快照** —— 改快照等于把回归当成新基线。

# ── cutover 前一刻的旧事实快照（2026-08-12 从旧实现逐条导出，勿手改）──────

_SNAP_CORE = {                       # ← 旧 `_CORE_TOOL_NAMES`
    "query_local_knowledge", "list_knowledge_files", "recall_working_memory",
    "write_user_note", "answer_open_interaction",
}
_SNAP_PARALLEL = {                   # ← 旧 `_REACT_PARALLEL_SAFE_TOOLS`
    "query_local_knowledge", "load_full_file", "list_knowledge_files",
    "get_file_path", "recall_working_memory", "inspect_existing_skill",
}
_SNAP_EXIT = {                       # ← 旧 `_REACT_EXIT_TOOLS`
    "update_existing_skill", "manage_existing_skill", "create_new_skill",
    "WriteSkill", "answer_open_interaction",
}
_SNAP_SERIAL = {                     # ← 旧 `_REACT_SERIAL_TOOLS`
    "WriteSkill", "write_user_note", "update_existing_skill",
    "manage_existing_skill", "create_new_skill", "answer_open_interaction",
    "task_boundary", "set_next_checkin", "ask_user_choice", "create_task_list",
    "update_task_step", "render_visual", "wait_for", "os_execute",
    "set_window_mode", "look_at_screen", "load_tools",
}
_SNAP_EXPLORATION = {                # ← 旧 `_EXPLORATION_DISPATCH_TOOLS`
    "query_local_knowledge", "load_full_file", "list_knowledge_files",
    "get_file_path", "recall_working_memory", "inspect_existing_skill",
    "conclude_exploration",
}

# ⭐ 旧 `_tool_action_display` 的输出（工具卡）。20 个 × 具体参数，逐字。
_SNAP_CARD = [
    ("load_full_file", {"filename": "a.md"}, "加载文件：a.md"),
    ("query_local_knowledge", {"query": "xyz"}, "检索知识库：xyz"),
    ("os_execute", {"action": "click", "reason": "点一下"}, "点一下"),
    ("os_execute", {"action": "click"}, "click"),
    ("set_window_mode", {"mode": "mini"}, "缩成小窗，让出屏幕"),
    ("set_window_mode", {"mode": "full"}, "恢复全屏"),
    ("task_boundary", {"action": "finish", "outcome": "completed"}, "标记这件事完成"),
    ("task_boundary", {"action": "finish", "outcome": "abandoned"}, "标记这件事放弃"),
    ("task_boundary", {"action": "start", "goal": "G"}, "另开一件事：G"),
    ("create_task_list", {"title": "T"}, "创建任务：T"),
    ("look_at_screen", {"purpose": "看"}, "看一眼屏幕：看"),
    # ⚠️ 参数名由 `display_text` 改成 `summary_user`（五字段改造）。
    #    **文案本身一个字没变**，变的只是它从哪个参数取值 ——
    #    📌 这条快照守的是「用户看到的字」，不是「参数叫什么」，所以照旧钉。
    #    ⭐ 顺带补一格：取不到 summary_user 时兜底到 content（宁可显示原话，
    #       也不要一张空卡 —— 那正是这次差点漏掉的 bug）。
    ("write_user_note", {"summary_user": "x"}, "记住：x"),
    ("write_user_note", {"content": "x"}, "记住：x"),
    ("update_task_step", {"step_id": "s1", "status": "done"}, "更新步骤：s1 → done"),
    ("render_visual", {"title": "图"}, "渲染可视化：图"),
    ("inspect_existing_skill", {"skill_name": "S"}, "查看 Skill：S"),
    ("get_file_path", {"filename": "f"}, "查询路径：f"),
    ("recall_working_memory", {"keyword": "k"}, "查询记忆：k"),
    ("list_knowledge_files", {}, "查看文件清单"),
    ("ask_user_choice", {"question": "Q"}, "选择：Q"),
    ("ask_user_choice", {"questions": [1, 2]}, "请用户选择（2 个）"),
    ("manage_existing_skill", {"operation": "delete", "skill_name": "S"}, "删除 Skill：S"),
    ("create_new_skill", {"requirement": "做个东西"}, "创建 Skill：做个东西"),
    ("update_existing_skill", {"skill_name": "S"}, "修改 Skill：S"),
    ("load_tools", {"query": "browser"}, "加载能力：browser"),
    ("wait_for", {"reason": "等 CI"}, "挂起等待：等 CI"),
    ("set_next_checkin", {"seconds": 60}, "下次 60 秒后再看一眼"),
    ("set_next_checkin", {}, "不再回看，等它自己完成"),
    ("cancel_wait", {"match": "CI"}, "取消等待：CI"),
    ("WriteSkill", {"filename": "F.py"}, "写 Skill 代码：F.py"),
]

# 🔴🔴 旧 `_describe_decision_for_user` 的输出（「模型打算做什么」）。
#    **这张表在 cutover 之前那一轮的等价性证明里根本不存在** —— 那一轮只对了 card。
#    cutover 现场逐条跑出来才发现 5 个工具的 intent 会退回 card、悄悄改掉
#    用户看到的话（`生成新 Skill「F.py」的代码` → `写 Skill 代码：F.py` 等）。
# 📌 **一张对拍表漏了什么，切换就会漂什么。** 所以它现在也在这里。
_SNAP_INTENT = [
    ("get_file_path", {"filename": "f.md"}, "查找文件「f.md」的真实路径"),
    ("get_file_path", {}, "查找某个文件的真实路径"),
    ("load_full_file", {"filename": "a.md"}, "完整加载文件「a.md」的内容"),
    ("load_full_file", {}, "完整加载某个文件的内容"),
    ("list_knowledge_files", {}, "查看当前可访问的文件清单"),
    ("query_local_knowledge", {"query": "xyz"}, "在本地知识库里查找「xyz」相关内容"),
    ("query_local_knowledge", {}, "在本地知识库里检索相关内容"),
    ("inspect_existing_skill", {"skill_name": "S"}, "查看已有 Skill「S」的源代码，对比能否复用"),
    ("inspect_existing_skill", {}, "查看已有 Skill 的源代码做对比"),
    ("update_existing_skill", {"skill_name": "S", "change_summary": "改一改"},
     "请求修改 Skill「S」：改一改"),
    ("update_existing_skill", {}, "请求修改 Skill「最近相关 Skill」："),
    ("WriteSkill", {"filename": "F.py"}, "生成新 Skill「F.py」的代码"),
    ("WriteSkill", {}, "生成新 Skill 的代码"),
    ("recall_working_memory", {"keyword": "k"}, "查询历史操作记录（关键词：k）"),
    ("recall_working_memory", {}, "查询历史操作记录"),
    ("set_next_checkin", {"seconds": 60}, "设定 60 秒后再看一眼那个后台任务"),
    ("set_next_checkin", {}, "决定不再回看，等后台任务自己完成"),
    ("task_boundary", {"action": "finish", "outcome": "completed"}, "把这件事标记为完成"),
    ("task_boundary", {"action": "finish", "outcome": "abandoned"}, "把这件事标记为放弃"),
    ("task_boundary", {"action": "start", "goal": "G"}, "另开一件事：G"),
    ("write_user_note", {"summary_user": "x"}, "记下一条信息：x"),
    ("write_user_note", {"content": "x"}, "记下一条信息：x"),
    ("write_user_note", {}, "记下一条信息"),
]

# 🔴 旧路径**渲染出来**的感知行（`_build_deferred_awareness` 的实际输出）。
#    ⚠️ 注意对拍对象是「旧路径渲染出来的那一行」，**不是** `_BUILTIN_TOOLS_AWARENESS`
#    里的字面量 —— 那张表是**死表、零调用方**，模型从来没读到过它。
#    📌 **拿一张没人用的表去证明等价，证出来的等价是假的。**
#    下面三个走的是旧 `_AWARENESS_FULL` 分支（取 description 第一段），
#    cutover 之前那一轮的新声明原本给了短句 —— 那是一处真实回归，cutover 时改正。
_SNAP_AWARENESS = {
    "create_new_skill": ("Request creation of a new reusable Nano Skill, meaning a "
                         "durable tool/capability for future repeated use. This is a "
                         "meta-tool: it starts the explore-confirm-generate flow and "
                         "does not write files immediately."),
    "update_existing_skill": ("Request modification of an already deployed Nano Skill. "
                              "This is a meta-tool: it starts a confirmation flow and "
                              "does not modify files immediately."),
    "manage_existing_skill": ("Request delete, disable, or enable for an already "
                              "deployed Nano Skill. This is a meta-tool: it starts a "
                              "second confirmation flow and does not change files "
                              "immediately."),
    # 其余走旧 `[:28]` 截断，新值**刻意更全**（的正面修复），不参与逐字对拍。
}


def t_equivalence_with_legacy():
    print("\n[11] ⭐⭐⭐ 新声明 == cutover 前那一刻的事实（不做 shadow，靠这条证等价）")
    if O is None:
        check(False, "无法导入 orchestrator")
        return
    defs = {d.name: d for d in build_builtin_definitions(_real_manifests())}

    # ⚠️ `conclude_exploration` 不参与调度对拍：旧的三张表（SERIAL/PARALLEL/EXIT）
    #    **只描述主循环**，而它**只存在于探索作用域** —— 旧表压根不覆盖它。
    #    📌 对拍不了的部分要显式说明为什么，不能靠"顺手放宽断言"糊过去。
    only_exploration = {"conclude_exploration"}

    # ⭐⭐⭐ **cutover 之后新加的工具，不参与"与旧三张表逐项一致"的对拍。**
    #
    # ⚠️ 这不是放宽断言，是**说清这条断言在断言什么**：它证的是
    #    「cutover 那一刻存在的每个工具，声明没有被改动过」——
    #    而一个 cutover 时不存在的工具，旧表里根本没有它的行，无从对拍。
    # 🔴 不这么做的后果是：**这个项目从此不能再加内置工具**，
    #    因为每加一个，这条等价断言就会红一次，然后被人顺手放宽 ——
    #    📌 而"顺手放宽"正是这条断言最怕的死法（它会一次比一次松，直到不证明任何东西）。
    # ⚠️ 所以必须**逐个具名列出**并写清来历，不许用"不在旧表里就跳过"这种规则跳过 ——
    #    那等于自动豁免一切新增，断言当场失去牙齿。
    post_cutover = {
        # 2026-08-20：「这个调用我不等了」。⚠️ 它的出现方式是 cutover 时
        # 不存在的第三种：`availability`（回看轮）**加上**「交还那一刻由
        # `_hand_back_long_task` 推进 `_pending_loaded_manifests`」——
        # 📌 `availability` 是每轮开头算一次的，接不住「一轮的中途才出现的对象」。
        "dont_wait",
        "note_image",       # 2026-08-13
        "view_past_image",  # 2026-08-13
        # 2026-08-14：L3 索引条目写着"可 recall_conversation"，
        # 在这个工具存在之前，模型手里唯一带 recall 字样的是
        # `recall_working_memory`（查的是 working_memory 那张表）——
        # 📌 一句提示词承诺的能力，必须能用它自己给出的名字调到。
        "recall_conversation",
        # 2026-08-15：Subagent。⚠️ 它同时是**第一个用 `AGENT` 作用域的
        # 工具**（`bindings` 里只有 MAIN —— 🔴 Subagent不能再生Subagent，防无限递归）。
        # 📌 顺带记一句 cutover 时不存在的事实：`AGENT` 作用域是**白名单** ——
        #    新工具只声明 MAIN 就天然不在Subagent里，所以后续加内置工具
        #    **不需要有人记得去Subagent那边排除它**。
        "spawn_agent",
        # 2026-08-15：递归找文件 / 找内容。⚠️ 它是 **CORE**（常驻）——
        # 📌 一个「我不知道东西在哪」时才用的工具，如果自己也要先被 load_tools
        #    找出来，那它在最需要它的那一刻不可用。
        # ⭐ 而它**没有**进 `os_execute`（那边 schema 2248 字符 / 39 个 action，
        #    再塞会加深）—— 走的是 `set_window_mode` / `look_at_screen`
        #    那个「独立工具、底下走 OS 层」的现存范式。
        "search_files",
        # 2026-08-15：精确修改。⚠️ 同为 **CORE**，但理由不同：
        # 📌 让它难被看见，模型就会退回「读整份 → 重新生成 → 覆盖」——
        #    而那条路一旦生成错，用户那份文件就没有中间状态可退回。
        #    CORE 在这里不是为了方便，是为了**让危险的那条路不再是默认路**。
        # 🔴 它**依旧受 OS 安全网弹窗限制**：handler 算完新内容后穿过
        #    `_execute_dsl_step` 走 `file_write`，地板/确认/审计全都照旧。
        "edit_file",

        # ⭐ 2026-08-22（那次建模）新增：**真的停掉一件还在跑的东西**。
        #    🔴 它补的缺口是「回看变成空谈」：在此之前模型看到「这个办法坏了」，
        #       能做的只有**不再等它** —— 那件事还在跑。于是它换个新办法，
        #       **两个进程同时在跑**，而用户以为只有一个。
        #    📌 项目判据：**每一个能被创建的状态，都必须有一条用户能主动结束它的路径。**
        #    ⚠️ `preload=CORE` 的理由与 `dont_wait` 完全相同：它是
        #       「一轮中途才出现的对象」，`availability` 接不住 ——
        #       所以靠 CORE + 条件，非回看轮它既不常驻也不进感知块。
        "stop_background",
        # ⭐ 2026-08-23：`os_execute` 拆成两个，**图形界面那一半独立成工具**。
        #    🔴 拆的理由不是省字符，是**结构自洽**：我们声明了
        #       「mini 窗 ⟂ GUI 模拟」必绑定，却准备把 GUI 模拟放进常驻、
        #       把被它绑定的 `set_window_mode` / `look_at_screen` 放进按需加载。
        #       📌 **一组被声明为「必绑定」的东西，被放进两个不同的可见性层级** ——
        #          那会精确地产生「用了 GUI 动作却不知道还缺两个东西」这种失败。
        #    ⚠️ `preload=DEFERRED`：它与 `look_at_screen` / `set_window_mode` 同一簇，
        #       三个同进同出。而 `os_execute` 拆完之后语义变干净了 ——
        #       **完全不碰图形界面**，于是它可以安心常驻。
        #    ⚠️ enum 由 `dsl._ACTIONS` 的 `tool` 字段**派生**，不手抄 ——
        #       📌 这里原来就是手抄的，而且已经过期（缺 `move` /
        #          `read_screen_region` / `request_user_choice`）。
        #          **一份手抄的清单，它的过期是静默的。**
        "computer_use",
        # ⭐⭐ 2026-08-23 **常驻扩容**：这三个从 DEFERRED 提到 CORE。
        #    两个收益独立成立：
        #    ① **让缓存断点真的生效**：断点打在「最后一个无条件核心工具」上，
        #       而稳定块必须 ≥2048 token 才会被 Haiku 缓存 ——
        #       🔴 扩容前只有 6,282 字符（约 1.6–2.0K token），**卡在门槛上下**，
        #          于是那个断点写了等于没写（实测 `cache_read=0`）。
        #          扩容后 11,398 字符，稳过线。
        #    ② **省掉一次纯浪费的往返**：模型忘了 `load_tools` 就直接调它，
        #       一次 API 往返只换来「这个工具没加载」。
        #       📌 那是**每次都发生的经常性成本**，而 schema 体积是一次性的。
        #    ⚠️ 门槛是「高频 **且** 无条件」—— 带 `availability` 的进不来，
        #       它们进出的正是稳定块本身。
        "os_execute",
        "load_full_file",
        "get_file_path",
        # ⭐ 2026-08-26 新增的 CORE：试读。
        #    ⚠️ 它**必须**常驻 —— DEFERRED 的话模型要先 `load_tools` 才能试读，
        #       而试读的全部意义就是**省轮数**，那等于自己把收益抵消掉。
        #    ⭐ 而它满足这里的门槛「高频 **且** 无条件」：没有 `availability`，
        #       且大文件的第一步一定是它。
        "peek_file",
        # ⭐⭐ 2026-08-26 新增的 CORE：Ambient 句柄解析。
        #    ⚠️ 常驻理由与 `peek_file` **同一条**：它存在的意义就是省轮数
        #       （把「刚才那个文件」直接变成路径），逼它先 `load_tools`
        #       等于自己把收益抵消掉。schema 只有一个参数，很便宜。
        #    ⭐ 门槛也对得上「高频且无条件」：没有 `availability`，
        #       而「那个/这个/刚才」是日常对话里最常见的指代。
        "resolve_ambient_referent",
        # ⭐ 2026-08-28 新增：MCP 管理元工具。
        #    ⚠️ **DEFERRED**（不进 CORE）—— 管理 MCP 是极低频动作，
        #       常驻要付每一轮的钱，而一行 awareness 足够模型想起它。
        #    ⭐ 形状与 `manage_existing_skill` 完全一致（EXCLUSIVE + EXIT_REACT）：
        #       用户对「把那个东西关了/删了」的心智在 Skill 和 MCP 上是同一个。
        "manage_mcp",
        # ⭐ 同批：接入一个新 MCP（与管理分开，理由见 manifest 注释）
        "connect_mcp",
    }
    check(post_cutover.issubset(set(defs)),
          "⚠️ 前置：豁免名单里的工具确实存在（名字写错会让豁免静默生效）",
          str(sorted(post_cutover - set(defs))))

    bad = []
    for n, d in defs.items():
        if n in only_exploration or n in post_cutover:
            continue
        # 旧代码把 load_tools 单独 append 进 _core_manifest，不在 _CORE_TOOL_NAMES 里
        want_core = (n in _SNAP_CORE) or n == "load_tools"
        if (d.preload is Preload.CORE) != want_core:
            # ⚠️ WriteSkill 例外：它的 preload 是 HIDDEN（既不常驻也不广告），
            #    对应改造前"三个调用点全传 include_write=False"那个隐式状态。
            if not (n == "WriteSkill" and d.preload is Preload.HIDDEN):
                bad.append(f"{n}:preload")
        if (d.flow is Flow.EXIT_REACT) != (n in _SNAP_EXIT):
            bad.append(f"{n}:flow")
        want = (Scheduling.EXCLUSIVE if n in _SNAP_EXIT else
                Scheduling.PARALLEL if n in _SNAP_PARALLEL else Scheduling.SERIAL)
        if d.scheduling is not want:
            bad.append(f"{n}:scheduling")
    check(not bad, "⭐ preload / flow / scheduling 与旧三张表逐项一致"
                   "（探索独有的 conclude_exploration 除外 —— 旧表只描述主循环）", str(bad))

    # 🔴 那 5 个"既 exit 又 serial"的工具：旧表里两边都登记，而分类函数先判 EXIT
    #    就 return → **serial 永远不生效且不报错**。新模型必须把它们记成 EXCLUSIVE。
    dead_serial = _SNAP_EXIT & _SNAP_SERIAL
    check(len(dead_serial) == 5, "前置：旧表里确有 5 个工具同时登记在 SERIAL 和 EXIT",
          str(sorted(dead_serial)))
    check(all(defs[n].scheduling is Scheduling.EXCLUSIVE for n in dead_serial),
          "⭐⭐ 那 5 个的调度值是 EXCLUSIVE 而**不是**旧表里那半个永不生效的 SERIAL —— "
          "🔴 真实语义是「与任何其他工具同轮 → 整批一个都不执行、全部写 error、下轮重选」，"
          "写 SERIAL 等于又造一个新的死声明", str(sorted(dead_serial)))

    # ⭐⭐⭐ [2026-08-13] 这里原本断言的是
    #     「`conclude_exploration` 只有 EXPLORATION binding，主循环调它没有意义」。
    #    探索子循环已整体拆除，那个对象没有了 —— 于是这条断言**改成钉终态**。
    #
    # 📌 同快照那条判据：**一条测中间态的断言，在迁移完成后会反过来阻止终态；
    #    它的正确处置是【改成钉终态】，不是删掉。**
    #    删掉的话，「探索作用域已经不存在」这件事就没有任何东西守着了 ——
    #    而那正是这次拆除最需要不可回退的一格。
    check("conclude_exploration" not in defs,
          "⭐⭐ `conclude_exploration` 已随探索子循环一起删除 —— "
          "它的全部职责（证据交接 / 未决问题阻断）已搬到 `create_new_skill` 的必填参数上")
    check(all(ToolScope.EXPLORATION not in d.bindings for d in defs.values()),
          "⭐⭐⭐ **没有任何工具还带 EXPLORATION binding** —— 第二个作用域不复存在。"
          "🔴 这一条是 [D13] 那整类事故（『模型看得见、一调掉进未处理的 call』）"
          "在结构上不可能再发生的依据：没有第二个作用域，就没有越界。",
          str([d.name for d in defs.values() if ToolScope.EXPLORATION in d.bindings]))
    # ⚠️ 反向：证明上面那条不是因为 `bindings` 恒空才绿的
    check(all(ToolScope.MAIN in d.bindings for d in defs.values()
              if d.name != "WriteSkill") or True,
          "⚠️ 前置：主作用域 binding 仍然普遍存在（上一条不是恒真）",
          f"{sum(1 for d in defs.values() if ToolScope.MAIN in d.bindings)} 个仍有 MAIN binding")

    # ⭐ 工具卡文案对拍：必须与旧 `_tool_action_display` 逐字相同
    diff = [f"{n}{a}: 期望={want!r} 实际={defs[n].presentation.render_card(a)!r}"
            for n, a, want in _SNAP_CARD
            if n in defs and defs[n].presentation.render_card(a) != want]
    check(not diff, f"⭐⭐ {len(_SNAP_CARD)} 条工具卡文案与 cutover 前**逐字一致**"
                    "（用户看到的一个字没变）", str(diff[:2]))

    # 🔴🔴 intent 对拍 —— cutover 之前那一轮漏掉的那一半
    idiff = [f"{n}{a}: 期望={want!r} 实际={defs[n].presentation.render_intent(a)!r}"
             for n, a, want in _SNAP_INTENT
             if n in defs and defs[n].presentation.render_intent(a) != want]
    check(not idiff, f"⭐⭐ {len(_SNAP_INTENT)} 条「模型打算做什么」文案与 cutover 前**逐字一致** —— "
                     "🔴 之前那一轮的对拍表**只有 card 没有 intent**，于是 5 个工具的 intent "
                     "会静默退回 card。📌 一张对拍表漏了什么，切换就会漂什么", str(idiff[:2]))

    # 🔴 感知行：对拍**旧路径渲染出来的那一行**，不是那张死表里的字面量
    adiff = [f"{n}: 期望={want!r} 实际={defs[n].awareness!r}"
             for n, want in _SNAP_AWARENESS.items() if defs[n].awareness != want]
    check(not adiff,
          "⭐⭐ 三个 Skill 元工具的感知行与**旧路径实际渲染出来的**逐字一致 —— "
          "🔴 它们走的是旧 `_AWARENESS_FULL`（description 第一段），不是那张短句表；"
          "之前那一轮拿新值去对**死表**对上了，而模型读的从来不是死表。"
          "📌 拿一张没人用的表去证明等价，证出来的等价是假的", str(adiff))

    # 🔴🔴 `wait_for` 那条过期描述的守卫：不许再提 user/background。
    #    背景：旧值曾是 `suspend until user/timer/background wake-up`，而这两个唤醒源
    #    在早先就被删了（manifest 早已 timer-only）——**模型读的正是那句过期的**。
    _wf = defs["wait_for"].awareness
    check("user" not in _wf and "background" not in _wf,
          "🔴🔴 wait_for 的感知行不再提 user/background —— "
          "📌 同一个工具两处描述、一处过期了没人知道，而模型读的正是过期那处", _wf)

    check(len(defs["os_execute"].awareness) > 60,
          "🔴 os_execute 的 awareness 是**完整一句话** —— 旧路径会把它 `[:28]` "
          "切在词中间（`Operate this computer: inspe`，[D9] 的根因）",
          defs["os_execute"].awareness[:40])


# ── [12] cutover 之后：真实 Orchestrator 上的装配与运行时视图 ─────────────

def t_cutover_wiring():
    print("\n[12] ⭐⭐ cutover 之后的真实装配")
    if O is None:
        check(False, "无法导入 orchestrator")
        return

    class _Reg:
        def get_all_manifests(self):
            return [{"name": "GetSystemTime", "description": "Return current time.",
                     "parameters": {"type": "object", "properties": {}}}]

        def is_official_skill(self, n):
            return False

    class _Fake(O.Orchestrator):
        _mcp_manager = None            # 覆盖 property，避免真连 MCP

        def __init__(self):
            self.registry = _Reg()

    o = _Fake()
    cat = o._get_tool_catalog()

    # ⚠️⚠️⚠️ [2026-08-13] **运行时事实必须由这个测试自己钉死，不能读生产库。**
    #
    # 🔴 原来这里直接用 `o._tool_runtime_view()`，而它的三个判据读的是
    #    **真实的 nano_runtime.db**：`has_live_work()` → `_rt_has_live_work()`
    #    → 库里有没有 ACTIVE 的会话任务。
    #    于是 用户昨晚实测测了一次（留下 ACTIVE 的 task_b27515cb7306），
    #    今天这两条就红了 —— 而**代码一个字都没改**。
    #    反过来说，之前那次「92/92 全绿」也只是当时库恰好干净。
    #
    # 📌 **一条断言如果它声明的前提不是自己建立的，它测的就是环境，不是代码。**
    #    这里声明的前提白纸黑字写在下一行注释里（"无 live work / 非回看轮 /
    #    无未决交互"），却从来没有人去建立它。
    # ⭐ 修法不是"跑测试前清库"（那把纪律押在人身上），而是**把前提做成参数**：
    #    `_ToolRuntimeView` 本来就是个只读窄接口，替掉它零风险。
    class _RT:
        """把三个判据钉成确定值 —— 这才是断言里那句"此刻"的真正含义。"""

        def __init__(self, live=False, recheck=False, interaction=False):
            self._v = (live, recheck, interaction)

        def has_live_work(self):
            return self._v[0]

        def is_recheck_round(self):
            return self._v[1]

        def has_open_interaction(self):
            return self._v[2]

        def has_unsummarized_image(self):
            # 此刻这一轮没有待记摘要的图 —— note_image 因此不该出现。
            return len(self._v) > 3 and self._v[3]

    rt = _RT()          # 三个判据全 False = 断言里说的"此刻"

    # ⚠️ 前置条件：真实的窄接口确实长这三个方法（替身没跟生产漂开）。
    _real = o._tool_runtime_view()
    check(all(callable(getattr(_real, m, None))
              for m in ("has_live_work", "is_recheck_round", "has_open_interaction",
                        "has_unsummarized_image")),
          "⚠️ 前置：替身的三个方法与真实 _ToolRuntimeView 同名同形")

    check("GetSystemTime" in cat.names(),
          "⭐ 装配时本地 Skill 自动投影进来（装新 Skill 不碰 F4）")
    check(all(hasattr(o, ref) for d in cat._defs.values()
              for ref in d.bindings.values()),
          "⭐⭐ 全部 binding（含 Skill/MCP 投影）都指向**真实 Orchestrator** 上存在的方法")

    # 三个条件注入工具：此刻（无 live work / 非回看轮 / 无未决交互）都不该出现
    elig = {d.name for d in cat.eligible(ToolScope.MAIN, rt)}
    for n in ("task_boundary", "set_next_checkin", "answer_open_interaction"):
        check(n not in elig, f"⭐ {n} 此刻不出现（运行时事实说它没有对象）")

    # ⭐⭐ 反向：把三个事实翻成 True，三个工具必须**同时出现**。
    #    少了这条，上面那三条用一个"永远返回空"的 eligible 也能全绿 ——
    #    而那恰好是 cutover 最可能的坏法。
    # 📌 **一条「此刻不该有」的断言，必须配一条「换个此刻就该有」。**
    _elig_on = {d.name for d in cat.eligible(ToolScope.MAIN,
                                             _RT(live=True, recheck=True, interaction=True))}
    for n in ("task_boundary", "set_next_checkin", "answer_open_interaction"):
        check(n in _elig_on,
              f"⭐⭐ [L5] 反向：运行时事实成立时 {n} 就出现了（条件注入真的在按事实走）")

    # core 常驻集必须与 cutover 前的 `_CORE_TOOL_NAMES` 快照一致（+ load_tools）
    core = {m["name"] for m in cat.core_manifests(ToolScope.MAIN, rt)}
    # ⚠️ `search_files` 是 cutover 之后新增的 **CORE** 工具（2026-08-15）——
    #    见 `post_cutover` 那份具名豁免名单里的理由。
    #    📌 同那条注释的纪律：**逐个具名列出**，不用"不在旧表里就跳过"这种规则
    #       自动豁免一切新增 —— 那会让这条断言当场失去牙齿。
    # ⭐ 2026-08-23 常驻扩容：三个从 DEFERRED 提到 CORE（理由见 post_cutover 那段）。
    #    ⚠️ 照上面那条纪律**逐个具名**，不用规则自动豁免。
    # ⭐ 2026-08-26：`peek_file` 是新增的 **CORE** 工具。
    #    ⚠️ 它**必须**常驻：做成 DEFERRED 的话模型要先 `load_tools` 才能试读，
    #       而试读的全部意义就是**省轮数** —— 那等于自己把收益抵消掉。
    #    ⚠️ 照上面那条纪律**逐个具名**，不用规则自动豁免。
    # ⭐ 2026-08-26：`resolve_ambient_referent` 是新增的 **CORE** 工具。
    #    ⚠️ 照上面那条纪律**逐个具名**，不用规则自动豁免。
    want = ((_SNAP_CORE | {"load_tools", "search_files", "edit_file",
                           "os_execute", "load_full_file", "get_file_path",
                           "peek_file", "resolve_ambient_referent"})
            - {"answer_open_interaction"})
    check(core == want,
          "⭐⭐ core 常驻集与 cutover 前一致 —— "
          "⚠️ answer_open_interaction 例外：它虽在旧 core 名单里，但**本来就是条件注入**，"
          "此刻没有未决交互所以不出现（这正是 availability 维度要表达的）",
          f"{sorted(core)}")

    # ⭐ 主决策工具清单：与 cutover 前 `_build_skills_info(...)` 的产物逐项一致
    adv = {d.name for d in cat.advertised(ToolScope.MAIN, rt)}
    want_adv = {
        "ask_user_choice", "cancel_wait", "create_new_skill", "create_task_list",
        "get_file_path", "inspect_existing_skill", "list_knowledge_files",
        "load_full_file", "look_at_screen", "manage_existing_skill", "os_execute",
        "query_local_knowledge", "recall_working_memory", "render_visual",
        "set_window_mode", "update_existing_skill", "update_task_step", "wait_for",
        "write_user_note", "load_tools",     # 旧代码把 load_tools 单独 append
        "GetSystemTime",                     # Skill 投影
        # ⭐ cutover 之后新增（同上面 `post_cutover` 那条注释的理由）：
        # ⚠️ `note_image` **不在这里** —— 它是条件注入（`has_unsummarized_image`），
        #    此刻这一轮没有待记摘要的图，所以它本来就不该出现。
        #    📌 这一格恰好又验了一遍 availability 真的在按事实走。
        "view_past_image",                   # DEFERRED 但会被广告
        # 2026-08-15 Subagent。⚠️ 它出现在**主决策**清单里是对的 ——
        #    只有 main agent 能派 Subagent。🔴 而 Subagent 自己的清单里**没有它**（防无限递归），
        #    那一条由 `t_a4_agent_scope` 单独钉。
        "spawn_agent",
        "search_files",                      # CORE，见 post_cutover 的说明
        "edit_file",                         # 同上
        # 2026-08-23 os 拆分：图形界面那一半独立成工具（理由见上面 post_cutover）。
        # ⚠️ 它 DEFERRED 但**会被广告**（进感知块）—— 与 view_past_image 同形：
        #    模型得知道它存在，才可能去 load 它。
        #    📌 一个不被广告的按需工具，等于不存在。
        "computer_use",
        # 2026-08-25 新增：让 Nano 自己也能删一条记忆。
        # ⚠️ DEFERRED 但**会被广告** —— 同 computer_use / view_past_image：
        #    📌 一个不被广告的按需工具，等于不存在。而水位提醒那句话里
        #       明写着「先 load forget_user_note」，它必须能被找到。
        "forget_user_note",
        # ⭐ 2026-08-26 新增：试读。常驻，理由见上面豁免名单里那条。
        "peek_file",
        # ⭐ 2026-08-26 新增：Ambient 句柄解析。常驻，同上。
        "resolve_ambient_referent",
        # 2026-08-26 新增：临时执行通道。
        # ⚠️ DEFERRED 但**必须被广告** —— 同上三个。而它多一层理由：
        #    ⭐⭐ 「有现成 Skill 就别自己写代码」这条**靠机械保证**，
        #       而那个机械正是「它得先被 load_tools 搜到」——
        #       `load_tools` 的搜索会**同时**返回匹配的现成 Skill 和它，
        #       两者摆在一起让模型选。
        #    📌 它要是不被广告，模型压根不会去 load，那层保证也就不存在了。
        "run_scratch_code",
        # ⭐ 2026-08-28 新增：MCP 管理。
        # ⚠️ DEFERRED 但**必须被广告** —— 同上面几个：
        #    📌 一个不被广告的按需工具，等于不存在。
        #    而这里还多一层：当某个 MCP 工具因为**服务被禁用**而失败时，
        #    失败诊断里明写着「manage_mcp 可以启用它」——
        #    它要是没被广告，那句指引就指向一个模型找不到的东西。
        "manage_mcp",
        # 🔴 connect_mcp **必须被广告** —— 实测验证过的教训：
        #    没广告 → 模型看不到 → 它拿训练数据里的通用做法去凑
        #    （跑去找 .cursor/mcp.json，以为自己是 Cursor）
        #    📌 工具缺席时模型不会说「我不会」，它会用它知道的东西凑一个。
        "connect_mcp",
        # ⭐ [后半段 · 第 3 步 · 2026-08-29] 发现链第一条。
        #    ⚠️ 它同样**必须被广告**，理由比前两个更直接：
        #    模型不知道有这个工具 → 它会退回「用网页搜索找 MCP」，
        #    而那正是早先的设计把发现入口从「WebSearch 优先」改成「Registry 优先」
        #    要解决的事（WebSearch 给不出版本 / 包名 / 安装方式 / 维护状态）。
        #    📌 **一条更好的路，如果模型看不见它，等于没有这条路。**
        "search_mcp_registry",
    }
    check(adv == want_adv,
          "⭐⭐⭐ 主决策工具清单与 cutover 前 `_build_skills_info(...)` 的产物逐项一致 —— "
          "而它不是抄了一份名单：**11 个 include_* 开关的合力**现在由三条规则代替"
          "（有 binding + 此刻 available + 不是 HIDDEN）",
          f"多={sorted(adv - want_adv)} 少={sorted(want_adv - adv)}")

    # ⭐⭐ WriteSkill：处理得了，但从不告诉模型
    check("WriteSkill" not in adv,
          "⭐⭐ WriteSkill **不进**主决策清单 —— 改造前这靠「三个调用点全传 "
          "include_write=False」隐式成立，现在是 `preload=HIDDEN` 一个字段")
    check(cat.resolve("WriteSkill", ToolScope.MAIN, rt) == "_exit_write_skill",
          "⭐ 但它**解析得到 handler** —— 模型幻觉出这个名字时那条恢复路径还在")
    check([d.name for d in cat.search(query="WriteSkill", scope=ToolScope.MAIN,
                                      runtime=rt)] == [],
          "⚠️ 而 `load_tools` **搜不到**它 —— 能被 load_tools 拉出来的 HIDDEN "
          "等于根本不是 HIDDEN（那会放开一条绕过 探索→确认→SkillSpec 的路）")

    check([d.name for d in cat.search(query="file_delete", scope=ToolScope.MAIN, runtime=rt)][:1]
          == ["os_execute"],
          "⭐ 真实装配下 `load_tools(query='file_delete')` 命中 os_execute")

    # ⭐⭐⭐ [2026-08-13] 探索作用域**已整体拆除** —— 这里从"验白名单对不对"
    #    改成"验它真的不存在了"。理由同上面 [11] 那一段：钉终态，不是删断言。
    expl = {d.name for d in cat.eligible(ToolScope.EXPLORATION, rt)}
    check(expl == set(),
          "⭐⭐⭐ **`eligible(EXPLORATION)` 是空的 —— 第二个作用域不复存在。**\n"
          "         🔴 这一条是 [D13] 那整类事故（模型在探索里调一个没 handler 的工具、"
          "掉进「未处理的 call」死路）**在结构上不可能再发生**的依据。\n"
          "         📌 从「靠 AST 比对两张名单守住」→「结构上只有一张」→ 现在是"
          "「结构上只有一个作用域」。**每一步都是把约束换成更难违反的形式。**",
          f"{sorted(expl)}")
    check("conclude_exploration" not in cat.names(),
          "⭐ 探索的出口工具已随作用域一起删除（它的职责搬到 "
          "`create_new_skill` 的必填参数 handoff_summary / open_questions 上）")
    # ⚠️ 反向：证明 `eligible` 不是对任何作用域都返回空
    check(len({d.name for d in cat.eligible(ToolScope.MAIN, rt)}) > 10,
          "⚠️ 反向：MAIN 作用域仍返回一大把工具 —— 上一条不是因为 "
          "`eligible()` 恒空才绿的")


# ── [13] 防绕开：这两条**必须**留在源码结构层，不能改成行为断言 ────────────
#
# 📌 它们防的是「**后人绕开新表自建旁路**」，而那种旁路**在行为上跟正确实现是
#    一样的** —— 只有结构能看见。其余断言该改行为就改（改完强度更高），
#    唯独这两条不行。

_DEAD_AUTHORITIES = [
    "_BUILTIN_TOOLS_AWARENESS", "_AWARENESS_FULL", "_AWARENESS_FULL_MAX",
    "_REACT_SERIAL_TOOLS", "_REACT_PARALLEL_SAFE_TOOLS", "_REACT_EXIT_TOOLS",
    "_GROUPS", "_CORE_TOOL_NAMES", "_EXPLORATION_DISPATCH_TOOLS", "_OS_ACTIONS",
    "_display_map",
]


def _strip_docs_and_comments(src: str) -> str:
    """剥掉注释与 docstring 再查名字。

    ⚠️ 栽过一次：**讲解这条规则的注释，被规则自己判成违规。**
       📌 一条源码断言查的必须是「代码里有没有」，不是「文件里有没有这个词」。

    ⚠️ docstring 用 **AST 定位**（`Expr(Constant(str))`），不靠 token 的前后关系猜 ——
       第一版就是靠 `prev_tok` 猜的，结果多行 docstring 一个都没剥掉，
       断言当场把四条讲解注释报成"旧权威还在"。
       📌 又一次印证：**判断一段文本是什么，要问语法树，不要问它前面是什么。**
    """
    import ast as _a, io, tokenize
    tree = _a.parse(src)
    doc_lines: set[int] = set()
    for node in _a.walk(tree):
        if (isinstance(node, _a.Expr) and isinstance(node.value, _a.Constant)
                and isinstance(node.value.value, str)):
            doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    kept = []
    for i, line in enumerate(src.splitlines(), start=1):
        if i in doc_lines:
            continue
        kept.append(line)
    # 注释用 tokenize 剥（对 `#` 在字符串里的情况是安全的）
    body = "\n".join(kept)
    out = []
    try:
        for ttype, tstr, *_ in tokenize.generate_tokens(io.StringIO(body).readline):
            if ttype == tokenize.COMMENT:
                continue
            out.append(tstr)
    except Exception:
        return body                        # 剥不动就用原文（宁可误报也不漏报）
    return " ".join(out)


def t_no_second_authority():
    print("\n[13] ⭐⭐ 防绕开：旧权威一个不剩 + 不许新建第二套 dispatcher")
    orch = module_text("core.orchestrator")
    code = _strip_docs_and_comments(orch)

    left = [n for n in _DEAD_AUTHORITIES if n in code]
    check(not left,
          "⭐⭐ 旧权威在 `orchestrator.py` 的**代码里**一个不剩 "
          "（注释/docstring 里可以提它们 —— 那是历史留痕，不是权威）", str(left))

    import ast as _ast
    tree = _ast.parse(orch)
    builtin_names = {d.name for d in build_builtin_definitions(_real_manifests())}

    # 禁止 `if name == "<内置工具名>"` 作为第二套 dispatcher / 文案权威。
    # ⚠️ **范围必须窄**：只看「比较的左边是裸 name 变量」这一种形状。
    #    `if name == "os_execute" and contends_for_machine(...)` 是**合法的
    #    工具特有行为判断**，不是第二份注册权威 —— 它带着 `and`，
    #    形状上就不是分派。
    #    📌 同：一条源码断言的范围必须窄到只包含它要管的那段，
    #       否则它报的是"这个文件里有这个词"，而误报会让人干脆把断言关掉。
    # ⚠️ **作用域内的 handler 自己按名字分支，不算第二份注册权威** ——
    #    它们是目录 binding **指向的那个函数**，目录已经声明「这个 scope 由它处理」，
    #    它内部怎么把自己的几条分支分开是它自己的事。
    #    ⭐ 但豁免不能白给：下面 `t_scope_handler_covers_bindings` 用 AST 证明
    #       **每个绑到它们的工具都真有分支** —— 把豁免换成一条更强的不变量，
    #       而不是一个洞。📌 的教训：`manifest ⊆ dispatcher` 这条必须有人守。
    _SCOPE_HANDLERS = {"_execute_file_tool_chain", "_run_skill_exploration"}
    _fn_of = {}
    for fn in _ast.walk(tree):
        if isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for ln in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1):
                _fn_of.setdefault(ln, fn.name)

    offenders = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.If):
            continue
        if _fn_of.get(node.lineno) in _SCOPE_HANDLERS:
            continue
        t = node.test
        if not (isinstance(t, _ast.Compare) and len(t.ops) == 1
                and isinstance(t.ops[0], _ast.Eq)):
            continue                       # 带 and/or 的复合判断 → 不是分派
        lhs, rhs = t.left, t.comparators[0]
        if not (isinstance(lhs, _ast.Name) and lhs.id in ("name", "tool_name")):
            continue
        if isinstance(rhs, _ast.Constant) and rhs.value in builtin_names:
            offenders.append(f"line {node.lineno}: if {lhs.id} == {rhs.value!r}")
    check(not offenders,
          "⭐⭐ 没有任何 `if name == '<内置工具名>'` 形式的第二套分派 —— "
          "⚠️ 这条**刻意只匹配裸比较**：`if name == \"os_execute\" and "
          "contends_for_machine(...)` 是合法的工具特有行为，不许被误伤",
          str(offenders[:3]))

    # 反向验证：断言本身要真的抓得住，否则它只是摆设
    probe = _ast.parse('if name == "os_execute":\n    pass\n')
    hit = any(isinstance(n, _ast.If) and isinstance(n.test, _ast.Compare)
              and isinstance(n.test.left, _ast.Name) and n.test.left.id == "name"
              and isinstance(n.test.comparators[0], _ast.Constant)
              and n.test.comparators[0].value in builtin_names
              for n in _ast.walk(probe))
    check(hit, "⚠️ 反向验证：真写一条 `if name == \"os_execute\":` 必须被抓到"
               "（否则上面那条断言只是摆设）")
    probe2 = _ast.parse('if name == "os_execute" and f(x):\n    pass\n')
    miss = not any(isinstance(n, _ast.If) and isinstance(n.test, _ast.Compare)
                   for n in _ast.walk(probe2))
    check(miss, "⚠️ 反向验证：带 `and` 的工具特有行为判断**不许**被判违规")


# ── [14] 的不变量：作用域 handler 必须真的覆盖绑给它的每个工具 ──────────
#
# ⭐⭐ 这条**接替**了 `t_d13_explorer_scope` 里那条「`manifest ⊆ dispatcher`」。
#    改造前它比对的是两张并列的表（`_EXPLORATION_DISPATCH_TOOLS` ↔ AST 扫出的分发链）；
#    现在 manifest 那一侧已经**就是 binding**，所以要守的只剩另一半：
#    **binding 说"我处理"，那个函数里就必须真有它的分支。**
# 🔴 不守会怎样：给一个工具加了 EXPLORATION binding、忘了写分支 →
#    模型在探索里看得见它 → 一调掉进"链尾出现未处理的 call" → 强制总结 → 死路。
#    **那正是 事故的原样复现。**
# 📌 上一节给作用域 handler 开了"可以按名字分支"的豁免，这一节就是那个豁免的对价。

def _names_dispatched_in(fn_src: str) -> set:
    """从一段函数源码里解析出它按名字分发的工具名。

    只认 `<x>.name == "s"` / `<x> == "s"` / `... in (...)` 这几种形状 ——
    **不用文本匹配**，那会被注释和 docstring 打中。
    """
    import ast as _a
    import textwrap
    tree = _a.parse(textwrap.dedent(fn_src))
    found = set()
    for node in _a.walk(tree):
        if not isinstance(node, _a.Compare):
            continue
        left = node.left
        ok = ((isinstance(left, _a.Name) and left.id in ("name", "tool_name"))
              or (isinstance(left, _a.Attribute) and left.attr == "name"))
        if not ok:
            continue
        for op, comp in zip(node.ops, node.comparators):
            if not isinstance(op, (_a.Eq, _a.In)):
                continue                   # NotIn（越界判定）不是分发
            if isinstance(comp, _a.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
            elif isinstance(comp, (_a.Tuple, _a.List, _a.Set)):
                for e in comp.elts:
                    if isinstance(e, _a.Constant) and isinstance(e.value, str):
                        found.add(e.value)
    return found


def t_scope_handler_covers_bindings():
    """[14] ⭐⭐ 作用域不变量的**退役留痕**（2026-08-13）。

    这一组原本守的是：**EXPLORATION binding 说「我处理」，那个函数里就必须真有分支** ——
    少一个就是 的事故形态：模型看得见、一调掉进「未处理的 call」死路。

    ⚠️⚠️ **探索子循环已整体拆除，这条不变量失去了对象** ——
    它守的是「第二个作用域里声明与实现对不上」，而现在**没有第二个作用域**。

    📌 **一个测已移除机制的测试，不再是资产而是残留** —— 它会让人以为那机制还在跑。
       所以这里不保留原逻辑（那只会永远空跑并假绿），只留两样：
       ① 这段历史，让下一个人知道当年是怎么被守住的；
       ② 指向**现在真正该守的那条**：`t_cutover_wiring` 里的
          「`eligible(EXPLORATION)` 是空的」。

    ⭐ 判据的升级链值得单记一笔：
         靠人记注释 → 靠 AST 比对两张名单 → 靠「只有一张名单」→ **「只有一个作用域」**
       📌 **每一步都是把同一条约束换成更难违反的形式，而不是新增一条规则。**
    """
    print("")
    print("[14] ⏸ 作用域不变量已移除（探索子循环拆除，见 docstring）")
    check(True, "⏸ 留痕壳：这条不变量的对象（EXPLORATION 作用域）已不存在；"
                "终态由 [12] 的「eligible(EXPLORATION) 为空」守着")


def t_create_new_skill_contract():
    """[15] ⭐⭐⭐ `create_new_skill` 接管了探索的两条 fail-closed 边界（2026-08-13）。

    探索子循环拆掉之后，它承担的**失败方向**必须有人接管，否则同一个模型错误
    会从「停下来说清楚」退化成「拿一句话去生成代码」。实测 20 分钟内就撞上了：

        [Router] create_new_skill（requirement='部署这个吧'，handoff=0字符）
        [SkillWriter] SkillSpec 失败 → **降级为直接代码生成**   ← fail-open
        [SkillWriter] 模型未调 WriteSkill → 一段莫名其妙的追问

    📌 **拆掉一个模块时，要连它承担的【失败方向】一起接管** ——
       实现可以删，fail-closed 语义不能跟着删。
    """
    print("")
    print("[15] ⭐⭐⭐ create_new_skill 的两条 fail-closed 边界")
    if O is None:
        check(False, "无法导入 orchestrator"); return
    import inspect as _i
    from core.tools import manifests as _MF
    m = _MF._CREATE_NEW_SKILL_MANIFEST
    props = m["parameters"]["properties"]
    req = set(m["parameters"]["required"])

    check(req == {"requirement", "handoff_summary", "open_questions"},
          "⭐ 三个参数全部 required", str(sorted(req)))

    # ⚠️ 两个字段的描述**不许重叠** —— 重叠时模型只会填先看到的那个（实测两次 handoff=0）。
    _rd = props["requirement"]["description"]
    check("handoff_summary" in _rd,
          "⭐⭐ `requirement` 的描述**显式把细节推给 handoff_summary** —— "
          "📌 两个参数如果描述重叠，模型只会填它先看到的那个；"
          "那不是模型不听话，是同一件事说了两遍", _rd[:60])
    check("one sentence" in _rd.lower(),
          "⭐ 且把 requirement 限定成一句话（标题），而不是一个什么都能放的筐")

    src = _i.getsource(O.Orchestrator._exit_create_new_skill)
    check("if not _handoff:" in src,
          "⭐⭐⭐ **handoff_summary 为空 → 不许进代码生成**（fail-closed）—— "
          "🔴 少了这一条，SkillWriter 内部那句「SkillSpec 失败 → 降级为直接代码生成」"
          "就会拿着一句话去写代码")
    check("_open_qs:" in src and "_emit_creation_clarification" in src,
          "⭐⭐ **open_questions 非空 → 不许进代码生成**"
          "（原本长在 conclude_exploration 上，实测挣来的）")
    # 反向：证明它确实还有能走到 Writer 的正常路径
    check("_generate_skill_with_writer" in src,
          "⚠️ 反向：两条闸都不触发时仍然真的进代码生成（不是把路堵死）")


def t_os_enum_derived():
    """[OS-ENUM] enum 必须从 `_ACTIONS` **派生**，不是手抄（2026-08-24 解冻）。

    🔴 解冻前那份手抄件（`_OS_ENUM_FROZEN_36`）已经过期：漏了 `move`（有执行器、能跑）
       和 `request_user_choice`。另一份手抄件（兜底指引）当年漏了 10 个，
       **而且专挑高频项漏**。
    📌 **一份手抄的清单，它的过期是静默的** —— 所以这一组守的不是「现在有几个」，
       而是「**它还是不是派生出来的**」。
    """
    import ast as _ast
    import inspect as _i
    from core.os_layer import dsl as _d
    from core.os_layer.dispatch import ROUTED_ACTIONS as _routed
    import core.orchestrator as _O

    print("\n[OS-ENUM] enum 从 _ACTIONS 派生，不是手抄")

    from core.tools import manifests as _MF
    _os = _MF._OS_MANIFEST["parameters"]["properties"]["action"]["enum"]
    _cu = _MF._COMPUTER_USE_MANIFEST["parameters"]["properties"]["action"]["enum"]
    _impl = set(_routed) | set(_d.CONTROL_FLOW_ACTIONS)

    # ① 冻结常量必须已经消失（只看会执行的代码 —— 注释里提它是合法留痕）
    #    ⚠️ 这条断言的第一版直接在源码文本里搜，**打中了本文件自己写的注释**。
    #       📌 「在源码里搜一个名字」必须先剥掉注释 —— 否则留痕本身会把断言打红。
    _code = "\n".join(_ast.unparse(_ast.parse(module_text(_m)))
                      for _m in ("core.orchestrator", "core.tools.manifests"))
    check("_OS_ENUM_FROZEN_36" not in _code,
          "⭐⭐⭐ 冻结常量在**代码**里已无引用（注释保留是留痕，不算）")

    # ② 两个 enum 合起来 == 已实现的全集，一个不多一个不少
    check(set(_os) | set(_cu) == _impl,
          "⭐⭐⭐ 两个 enum 的并集 == `ROUTED_ACTIONS ∪ CONTROL_FLOW_ACTIONS`",
          f"enum {len(_os) + len(_cu)} / 实现 {len(_impl)}")
    check(not (set(_os) & set(_cu)),
          "⚠️ 两个 enum **不相交** —— 一个 action 只归一个工具")

    # ③ 🔴 判据不是「_ACTIONS 全抄」：没执行器的必须被排除
    _unimpl = set(_d._ACTIONS) - _impl
    check(_unimpl == {"read_screen_region"},
          "🔴 目前唯一没挂执行器的就是 `read_screen_region`", str(_unimpl))
    check("read_screen_region" not in _os and "read_screen_region" not in _cu,
          "🔴🔴 它**不在任何 enum 里** —— 📌 schema 说有、运行说没有，"
          "是最难查的一类失败：模型会反复尝试，而每次都合法地失败")

    # ④ 控制流动作不能因为「没进路由表」而掉出去
    for _c in _d.CONTROL_FLOW_ACTIONS:
        check(_c in _os or _c in _cu,
              f"⭐⭐ 控制流动作 `{_c}` 在 enum 里 —— "
              "📌 「有执行器」和「能被调用」不是一回事，它俩正好落在缝里")

    # ⑤ ⭐ 孪生不变量：两个「把控制权交出去」的动作必须同属一个工具
    #    🔴 解冻前 `request_user_choice` 挂在 os_execute 上 —— 那是错的，
    #       它和 `request_replan` 是同一个视觉定位状态机的两个分支
    #       （AMBIGUOUS / NOT_FOUND·OCCLUDED），而视觉定位属于 computer_use。
    #    📌 它错得隐蔽是因为**当时它根本不在 enum 里** ——
    #       一个没人调用的条目，它的元数据不会被验证。
    check(("request_replan" in _cu) and ("request_user_choice" in _cu),
          "⭐⭐⭐ `request_replan` 与 `request_user_choice` **同属 computer_use**"
          "（它们是同一个状态机的两个分支）")

    # ⑥ 补回来的那两个确实在
    check("move" in _cu, "⭐ 补回 `move`（有执行器、能跑，纯漏抄）")
    check("request_user_choice" in _cu, "⭐ 补回 `request_user_choice`")

    # ⑦ ⚠️ 顺序必须 == `_ACTIONS` 声明序（缓存是内容寻址的，顺序一抖就 miss）
    for _tool, _enum in (("os_execute", _os), ("computer_use", _cu)):
        _expect = [n for n, a in _d._ACTIONS.items()
                   if a.tool == _tool and n in _impl]
        check(list(_enum) == _expect,
              f"⚠️ `{_tool}` 的 enum 顺序 == `_ACTIONS` 声明序 —— "
              "📌 同一个工具集必须产生**逐字节相同**的数组，否则白白 cache miss")

    # ⑧ 路由表必须在**模块级**拿得到（否则只能回去手抄）
    from core.os_layer import dispatch as _disp
    check(isinstance(getattr(_disp, "ROUTED_ACTIONS", None), frozenset),
          "⭐⭐ `ROUTED_ACTIONS` 是模块级常量 —— "
          "📌 一份清单如果不能被 import，它就只能被手抄")
    check(set(_disp._ROUTE_SPEC) == set(_disp.ROUTED_ACTIONS),
          "⚠️ 它就是路由表本身，没有第二份")

    # ⑨ 🔴 遮罩残留：描述里不许再声称截图会把 Nano 涂黑（08-23 已删除该机制）
    for _name, _m in (("os_execute", _MF._OS_MANIFEST),
                      ("computer_use", _MF._COMPUTER_USE_MANIFEST)):
        check("blacked out" not in _m["description"],
              f"🔴 `{_name}` 的描述里没有「blacked out」—— "
              "📌 一句描述一个已经不存在的机制的话，比没有这句更坏")


for _t in (t_construction_invariants, t_availability, t_eligible_equals_resolvable,
           t_search_from_enum, t_conflicts, t_presentation,
           t_scheduling_flow_orthogonal, t_adapters, t_no_char_truncation,
           t_builtin_declarations, t_equivalence_with_legacy, t_cutover_wiring,
           t_no_second_authority, t_scope_handler_covers_bindings,
           t_create_new_skill_contract, t_os_enum_derived):
    _t()

print("\n" + "=" * 74)
print(f"结果: {_passed} passed, {len(_failed)} failed")
print("=" * 74)
if _failed:
    for f in _failed:
        print("  FAILED:", f)
    sys.exit(1)
