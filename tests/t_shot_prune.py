# -*- coding: utf-8 -*-
"""截图目录回收 —— 按**体积**（2026-08-25 重写），以及两次没测出来的原因。

═══ 这一套守的东西 ═══

  ① ⭐⭐ **回收由「预算」触发，不由「张数」触发。**
     🔴 旧规则是「留最近 20 张」。2026-08-25 否掉：
        > 「20 张和 21 张各自的语义是什么？30 张跟 20 张的语义又是什么，
        >   30 = 大？那 20 难道就能说是小吗」
     📌 **这个数的语义不是「最多占多少」，是「一个长期使用 Nano 的用户，
        要永久让出多少磁盘」** —— 它只增不减，长期用就一定停在这个值上，
        也就是 README 里那行「推荐预留空间」的实际取值。
        ⇒ 而「20 张」写不出那一行：20 张可能是 4MB，也可能是 200MB。
     ⭐ 实测佐证：真目录 47 张只占 52.4 MB —— **旧规则会为了一个没有意义的
        数字，删掉 27 张完全正常的证据。**

  ② 🔴🔴🔴 **触发点必须挂在「拥有这个目录的人」身上，不是「往里写的人」。**
     同一条教训栽了两次，两次都在 `executor_low._prune_shots`：
       · 2026-08-23：glob 写成 `shot_*.png`，真实文件名是 `locate_*.png`
         → **一张都没匹配到**，清理跑了等于没跑。
       · 2026-08-25：glob 修对了，但**触发仍然挂在「保存 shot_* 时」**。
         而 `shot_*` 全目录只有 3 张，`locate_*`/`annotated_*` 有 44 张
         → 清理这辈子只跑过 3 次，实测 47 张 / 52.4 MB（上限写着 20）。
     📌 **扫描范围和触发时机是两件事** —— 修了前一件不等于修了这条教训。
     ⇒ 现在挂在 `OSAuditLogger.record()` 上：每个 OS 动作都会写审计，
        这是一条**没有例外**的心跳；而目录本来就是审计层自己的。

  ③ 🔴🔴 **上一版的测试给了假信心 —— 它验的是问题的另一半。**
     那条断言的注释原文：
        > 第三处在 dispatch.py（标注图），它跟 executor_low 共用同一个目录，
        > **所以下一次任意一次截图就会把它一起清掉**
     「任意一次截图」是假的 —— 代码里是「下一次 **executor_low 保存 shot_***」，
     而纯视觉定位的会话一次都不会有。
     📌 那条断言证明了「**扫描范围**是对的」，却把「**触发会不会发生**」
        当成理所当然写进了注释。它通过了，而 bug 还在。
     ⚠️ **一条验的是问题另一半的测试，给的是假信心 —— 比没有测试更坏。**
     ⇒ 所以本文件现在**直接断言触发点在 audit 层**，并且**跑真实回收**验行为，
        不再靠「某处调了几次某函数」这种代理指标。

  ④ ⭐ 三条规则各答一个不同的问题（2026-08-25 定，落地时又补了两个洞）：
       没超预算 → 一张都不删     ← **没有成本被付出，就没有回收的理由**
       ③ 异常优先删              ← 单张吃掉预算 10%，它同时也是坏证据
       ① 再按最旧删
       ② 地板：不许削到 N 张以下  ← **证据 > 磁盘**
     🔴 落地时抓到的两个洞（讨论时都没覆盖）：
       · 把「它是异常」当成了**独立的删除理由** → 总量 150MB（预算 200MB）
         时也把 5 张 30MB 的图全删光 → 目录归零，而磁盘上根本没有压力。
         📌 「吃预算」是**触发条件**，「是坏证据」只是**该删谁**的排序理由。
       · 异常豁免条件写成「正常图不足地板」→ 用户那个例子直接反过来：
         9 张 1MB 好证据 + 1 张 200MB 异常 → 9 < 10 → **留下那张坏的**。
         📌 有 9 张正常图在，异常就是**可辨认的**；只有一张正常的都没有时，
            才轮到「坏证据 > 零证据」。⇒ 条件改成「一张正常图都没有」。

  ⑤ 🔴🔴🔴 **实测「封顶失败」，而真相是这条路根本没被走到。**（2026-08-24 原样保留）
     用户让 Nano 截图 → 目录里一张新的都没有 → 结论「没保存到 screenshots/」。
     核实下来这个观察是对的，但原因不是漏写：
       · `look_at_screen`（Nano「看屏幕」那条）**从头到尾不落盘** ——
         `_capture_screen_image` → `_pil_to_png` → **bytes** → base64 进事件队列，
         而 `screenshot_preview` 在 `_CHAT_EVENTS_EPHEMERAL` 里（重启不回放）。
       · 落盘的是**另一族**：`computer_use → screenshot` / 视觉定位 `locate`。
     ⭐⭐⭐ **用户给的才是判据**：
        > 「普通截图不需要落盘。落盘的截图是因为它在 **os_audit** 文件夹下，
        >   而只有 computer use 的定位截图需要**保存证据**后续审计。」
        ⇒ 落盘与否**不由「是不是截图」决定，由「是不是证据」决定**。
     ⚠️ 用**断言**钉住而不是写进注释：
        📌 一个只写在注释里的事实，下一次还会被当成 bug 重查一遍。

用法：
  py -3.10 tests\t_shot_prune.py
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401
from tests._src import module_text  # noqa: E402

from loguru import logger
logger.remove()

MB = 1024 * 1024
_results: list[tuple[bool, str, str]] = []


def _code_only(path: pathlib.Path) -> str:
    """剥掉注释 —— 断言不能被我自己写的留痕喂饱。"""
    try:
        return ast.unparse(ast.parse(path.read_text(encoding="utf-8")))
    except SyntaxError:
        return path.read_text(encoding="utf-8")


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def _fresh():
    """一个干净的审计目录 + 造图助手。"""
    from core.os_layer.audit import OSAuditLogger
    al = OSAuditLogger(log_dir=pathlib.Path(tempfile.mkdtemp()))
    sd = al.screenshot_dir

    def mk(name: str, size: int, mtime: float):
        f = sd / name
        f.write_bytes(b"x" * size)
        os.utime(f, (mtime, mtime))
        return f

    def clear():
        for f in sd.glob("*.png"):
            f.unlink()

    return al, mk, clear


# ══════════════════════════════════════════════════════════════════════════
def t_budget_is_the_trigger() -> None:
    print("\n[1] ⭐⭐ 回收由【预算】触发，不由张数")
    al, mk, clear = _fresh()

    # 📌 没有成本被付出，就没有回收的理由 —— 哪怕 500 张。
    clear()
    many = [mk(f"a{i:03d}.png", 100 * 1024, 1000 + i) for i in range(500)]
    r = al.prune_screenshots()
    check(r["deleted"] == 0 and r["kept"] == 500,
          "⭐⭐⭐ 500 张但只占 50MB → **一张都不删**（旧规则会删掉 480 张）",
          f"删 {r['deleted']}")

    # 反过来：张数少不等于占得少。15 张就能超预算。
    clear()
    fat = [mk(f"b{i:02d}.png", 15 * MB, 2000 + i) for i in range(15)]   # 225MB
    r = al.prune_screenshots()
    check(r["deleted"] > 0 and r["total"] <= al._SHOT_BUDGET_BYTES,
          "⭐ 15 张就占了 225MB → 超预算要删（**张数少不等于占得少**）",
          f"删 {r['deleted']}，剩 {r['total'] // MB}MB")
    check(not fat[0].exists() and fat[-1].exists(), "删的仍然是最旧的")

    # ⚠️ 由设计推出来的一条边界，**故意钉住**：
    #    文件数 ≤ 地板时，**无论多大都不会被删**。
    # 📌 那不是 bug，是「证据 > 磁盘」的直接后果：只有 5 张图时，
    #    删掉任何一张的代价都高于超预算。而 WARNING 日志会说明
    #    「截图本身可能出了问题」—— 那才是这时候该修的东西。
    clear()
    huge = [mk(f"h{i}.png", 60 * MB, 7000 + i) for i in range(5)]       # 300MB
    r = al.prune_screenshots()
    check(all(f.exists() for f in huge),
          "⚠️ 只有 5 张（< 地板 10）时，哪怕 300MB 也一张不删 —— "
          "这是「证据 > 磁盘」的直接后果，不是 bug")


def t_three_rules() -> None:
    print("\n[2] ⭐⭐ 三条规则各自答一个不同的问题")
    al, mk, clear = _fresh()
    B = al._SHOT_BUDGET_BYTES

    # ① 超预算 → 删最旧
    clear()
    n = [mk(f"n{i:03d}.png", 1 * MB, 3000 + i) for i in range(250)]
    r = al.prune_screenshots()
    check(r["total"] <= B, "① 删到预算内", f"{r['total'] // MB}MB")
    check(not n[0].exists() and n[-1].exists(),
          "① 删的是**最旧**的（审计证据里新的更有用）")

    # ③ 异常优先，且正常图一张不动
    clear()
    nm = [mk(f"d{i:03d}.png", 1 * MB, 6000 + i) for i in range(190)]
    big = mk("huge.png", 50 * MB, 6100)
    al.prune_screenshots()
    check(not big.exists(), "③ 异常（>10% 预算）被**优先**删")
    check(all(f.exists() for f in nm),
          "⭐⭐⭐ 那个例子：正常证据**一张没动** —— "
          "只有 ①② 的话会反过来，删掉好的、留下坏的")

    # ② 地板：全是异常时也不许清空
    clear()
    tw = [mk(f"c{i:02d}.png", 30 * MB, 5000 + i) for i in range(12)]
    r = al.prune_screenshots()
    check(r["kept"] == al._SHOT_FLOOR,
          f"② 全是异常且超预算 → 地板兜住最近 {al._SHOT_FLOOR} 张（**证据 > 磁盘**）",
          f"剩 {r['kept']}")
    check(tw[-1].exists() and not tw[0].exists(), "② 留的是最新的")


def t_two_holes_found_at_landing() -> None:
    print("\n[3] 🔴🔴 落地时抓到的两个洞（讨论时都没覆盖）")
    al, mk, clear = _fresh()

    # 洞一：把「它是异常」当成独立的删除理由
    clear()
    five = [mk(f"b{i}.png", 30 * MB, 4000 + i) for i in range(5)]   # 150MB < 200MB
    r = al.prune_screenshots()
    check(all(f.exists() for f in five),
          "🔴 150MB 没超预算 → 5 张 30MB 的图**一张都不删** "
          "（第一版会全删光，目录归零）")
    # 📌 「吃预算」是触发条件，「是坏证据」只是该删谁的排序理由。

    # 洞二：异常豁免条件写成「正常图不足地板」
    clear()
    good = [mk(f"g{i}.png", 1 * MB, 2000 + i) for i in range(9)]     # 9 < 地板 10
    bad = mk("bad.png", 200 * MB, 2100)
    al.prune_screenshots()
    check(not bad.exists() and all(f.exists() for f in good),
          "🔴🔴 9 张好证据（不足地板）+ 1 张异常 → 删异常、留 9 张 "
          "（第一版会为了凑地板**留下那张坏的**）")


def t_trigger_lives_in_audit() -> None:
    print("\n[4] 🔴🔴🔴 触发点在 audit（拥有目录的人），不在写入方")
    audit_src = _code_only(ROOT / "core" / "os_layer" / "audit.py")
    low_src = _code_only(ROOT / "core" / "os_layer" / "executor_low.py")

    check("prune_screenshots" in audit_src, "回收实现在 audit.py")
    check("_maybe_prune_screenshots" in audit_src, "有一条节流后的心跳")

    # ⭐ 心跳必须挂在 `record()` 里 —— 那是唯一没有例外的通路。
    tree = ast.parse(module_text("core.os_layer.audit"))
    rec = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "record"), None)
    check(rec is not None, "找得到 record()")
    if rec is not None:
        check("_maybe_prune_screenshots" in ast.unparse(rec),
              "⭐⭐⭐ 心跳挂在 `record()` 上 —— 每个 OS 动作都会写审计，**没有例外**")

    # 🔴 而旧实现必须消失，否则就是两份判据（迟早有不同的预算）
    check("_prune_shots" not in low_src,
          "⭐⭐ executor_low 里的旧回收**已删除**（判据只能有一处）")
    check("_SHOT_KEEP" not in low_src, "⭐ 旧的「留 20 张」常量已删除")
    check("🪦" in module_text("core.os_layer.executor_low"),
          "🪦 删除处留了墓碑，写清为什么别加回来")


def t_heartbeat_is_real() -> None:
    """⭐⭐ 心跳端到端：**只调 `record()`**，回收要自己发生。

    🔴 这一组补的是「手动调 `prune_screenshots()` 跑通」**证明不了**的那一半 ——
       两次 bug 都不在回收逻辑里，都在「有没有人来调它」。
       📌 一个只验回收逻辑的测试，恰好跳过了唯一出过问题的那一半。
    """
    print("")
    print("[6] ⭐⭐ 心跳端到端（只调 record，不碰 prune）")
    from core.os_layer.audit import OSAuditLogger
    al = OSAuditLogger(log_dir=pathlib.Path(tempfile.mkdtemp()))
    sd = al.screenshot_dir
    for i in range(250):
        f = sd / f"locate_{i:04d}.png"
        f.write_bytes(b"x" * MB)
        os.utime(f, (1000 + i, 1000 + i))
    before = len(list(sd.glob("*.png")))

    n = al._PRUNE_EVERY_N_RECORDS
    for _ in range(n - 1):
        al.record(action="click", params={}, effective_risk=2, result_status="success")
    mid = len(list(sd.glob("*.png")))
    check(mid == before,
          f"⚠️ 第 {n - 1} 条审计时还没到节流点 → 一张没删（节流是真的）", f"{mid}")

    al.record(action="click", params={}, effective_risk=2, result_status="success")
    after = list(sd.glob("*.png"))
    total = sum(f.stat().st_size for f in after)
    check(len(after) < before,
          f"⭐⭐⭐ 第 {n} 条审计触发回收 —— **全程没有任何人调过 prune_screenshots()**",
          f"{before} → {len(after)}")
    check(total <= al._SHOT_BUDGET_BYTES,
          "⭐ 判据是【总量 ≤ 预算】，不是【剩几张】", f"{total // MB}MB")

    # 🔴 `_prune_tick` 是**实例属性** —— 如果哪天变成「每个动作新建一个 logger」，
    #    计数器会永远从 0 开始，心跳永远跳不到节流点。
    #    ⚠️ 2026-08-26 实测验过这条是好的（Nano 点一下 → 210MB 收到 200MB），
    #       但那靠的是人去跑一次；这里用断言钉住。
    check(hasattr(al, "_prune_tick") and al._prune_tick >= n,
          "⭐ 计数器在同一个实例上累加（每次新建 logger 的话它永远到不了节流点）",
          f"tick={getattr(al, '_prune_tick', None)}")

    # ⚠️ 节流值必须是 20 —— 实测测试时会被临时改成 1。
    # 📌 让「忘了改回来」变成一个**会报错**的状态，而不是靠记性。
    check(al._PRUNE_EVERY_N_RECORDS == 20,
          "⚠️⚠️ 节流值是 20（实测时会临时改成 1，这条防止忘了改回来）",
          str(al._PRUNE_EVERY_N_RECORDS))


def t_look_only_actions_write_no_audit() -> None:
    """⭐ 「看一眼」既不落盘、也不进审计 —— 而这两条是同一条判据。"""
    print("")
    print("[7] ⭐ 只有【动了电脑】才进审计（2026-08-26）")
    # 「单纯截图不算 gui 操作，他们不需要记录，进去反而是**证据噪音**，
    #             因为他们没有任何跟实际操作电脑对等的含义」。
    # ⭐ 与 08-24 那条判据是同一条的两个应用：
    #      落盘与否  不由「是不是截图」决定，由「是不是证据」决定
    #      审计与否  不由「是不是动作」决定，由「是不是动了电脑」决定
    # ⭐⭐ 而这带来一个自洽性：**心跳只在真动电脑时跳，而产图的正是同一批动作**
    #     ⇒「不动电脑 → 不产图 → 不需要回收」，回收的覆盖面天然是对的。
    orch = module_text("core.orchestrator")
    tree = ast.parse(orch)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_handle_look_at_screen"), None)
    if fn is None:
        check(True, "（没有 _handle_look_at_screen，跳过）")
        return
    u = ast.unparse(fn)
    check(".record(" not in u,
          "⭐⭐ `look_at_screen` 不写审计（它没动电脑 → 进去是证据噪音）")
    check("_screenshot_dir" not in u and "screenshot_dir" not in u,
          "⭐ 它也不落盘（08-24 已确认，这里一并钉住）")


def t_prune_never_breaks_screenshot() -> None:
    print("\n[5] ⚠️ 回收是家务，不该有能力让主功能失败")
    src = module_text("core.os_layer.audit")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_maybe_prune_screenshots"), None)
    check(fn is not None, "找得到 _maybe_prune_screenshots")
    if fn is not None:
        check(any(isinstance(x, ast.Try) for x in ast.walk(fn)),
              "⭐ 它整个包在 try 里（回收炸了不许影响审计）")

    # 真跑一次：目录不存在也不许抛
    from core.os_layer.audit import OSAuditLogger
    al = OSAuditLogger(log_dir=pathlib.Path(tempfile.mkdtemp()))
    import shutil
    shutil.rmtree(al.screenshot_dir, ignore_errors=True)
    try:
        r = al.prune_screenshots()
        check(r["deleted"] == 0, "⭐ 目录被删掉了也只是返回 0，不抛异常")
    except Exception as e:
        check(False, "⭐ 目录被删掉了也只是返回 0，不抛异常", type(e).__name__)


def t_look_at_screen_does_not_write_disk() -> None:
    print("")
    print("[5] 🔴🔴🔴 「截图没进 screenshots/」—— 是真的，且**不是 bug**")
    orch = module_text("core.orchestrator")
    tree = ast.parse(orch)

    # ① _capture_screen_image 返回 PIL 图，_pil_to_png 返回 bytes —— 全程不落盘
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_pil_to_png"), None)
    check(fn is not None, "找得到 _pil_to_png")
    if fn is not None:
        body = ast.dump(fn)
        check("BytesIO" in body,
              "⭐⭐ _pil_to_png 存进 **BytesIO**，不是文件")
        # `im.save(b, "PNG")` 的目标是 BytesIO，不是路径
        check("_screenshot_dir" not in ast.unparse(fn),
              "⚠️ 它一个字都没提 screenshot_dir")

    # ② 图是靠事件推上屏的
    emit = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_emit_shot"), None)
    check(emit is not None, "找得到 _emit_shot")
    if emit is not None:
        u = ast.unparse(emit)
        check("b64encode" in u and "screenshot_preview" in u,
              "⭐⭐ 走 base64 + 事件队列上屏，**不落盘**")
        check("open(" not in u and "write" not in u,
              "⚠️ 里面没有任何写文件动作")

    # ③ 而且它刻意不持久化 —— 这是 用户自己定的
    app_src = module_text("app")
    i = app_src.find("_CHAT_EVENTS_EPHEMERAL")
    j = app_src.find("_CHAT_EVENTS_INTERACTION")
    check(0 < i < j and '"screenshot_preview"' in app_src[i:j],
          "⭐⭐⭐ screenshot_preview 在 **EPHEMERAL** 表里：重启不回放、也不存")

    print("     ↳ 结论：让 Nano「看一眼屏幕」不会产生任何文件；")
    print("       产生文件的是 computer_use→screenshot 与视觉定位 locate。")


# ══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 74)
    print("截图目录回收（按体积） + 「哪条路才落盘」")
    print("=" * 74)
    t_budget_is_the_trigger()
    t_three_rules()
    t_two_holes_found_at_landing()
    t_trigger_lives_in_audit()
    t_prune_never_breaks_screenshot()
    t_heartbeat_is_real()
    t_look_only_actions_write_no_audit()
    t_look_at_screen_does_not_write_disk()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
