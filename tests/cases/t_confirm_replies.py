# -*- coding: utf-8 -*-
"""确认类事件的回复登记表（core/runtime/replies.py）与事件可序列化。

- 登记表语义：按 reply_id + 动作名调用回调；未知 id、已撤销的 id、未登记的动作都返回 False 不抛错；
  `pending` 退出时撤销；`reply_callback` 只包装事件声明过的动作。
- 后端发出的事件字典里不含可调用对象：不出现 `on_*` 键，也没有 lambda 值。
- 界面侧不再从事件里取 `on_*` 回调。
- 运行时检查（core/runtime/wire.py）：找出事件里不是 JSON 兼容类型的值，同一问题只记一次。

用法：
  py -3.10 tests\\cases\\t_confirm_replies.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tests._console  # noqa: F401,E402
from tests import _src as S  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_registry() -> None:
    print("\n▶ 登记表语义")
    from core.runtime import replies as R

    got: list = []
    rid = R.register({"confirm": lambda: got.append("c"),
                      "choice": lambda v: got.append(("v", v))})
    check(R.actions_of(rid) == ["choice", "confirm"], "actions_of 列出登记的动作", str(R.actions_of(rid)))
    check(R.resolve(rid, "confirm") is True and got == ["c"], "resolve 调用对应回调")
    check(R.resolve(rid, "choice", "B") is True and got[-1] == ("v", "B"), "resolve 把参数传给回调")
    check(R.resolve(rid, "nope") is False, "未登记的动作返回 False")
    R.discard(rid)
    n = len(got)
    check(R.resolve(rid, "confirm") is False and len(got) == n, "撤销后的回复被忽略，回调不再调用")
    check(R.resolve("no-such-id", "confirm") is False, "未知 id 返回 False")
    check(R.actions_of(rid) == [], "撤销后 actions_of 为空")

    with R.pending({"cancel": lambda: got.append("x")}) as rid2:
        check(R.resolve(rid2, "cancel") is True, "pending 期间可回复")
    check(R.resolve(rid2, "cancel") is False, "pending 退出后自动撤销")

    try:
        R.register({})
        empty_raises = False
    except ValueError:
        empty_raises = True
    check(empty_raises, "空登记抛 ValueError")
    try:
        R.register({"confirm": "not callable"})
        bad_raises = False
    except TypeError:
        bad_raises = True
    check(bad_raises, "非可调用对象抛 TypeError")

    rid3 = R.register({"approve": lambda: got.append("a"), "reject": lambda: None})
    ev = {"event": "mini_auth_request", "reply_id": rid3, "actions": ["approve", "reject"]}
    cb = R.reply_callback(ev, "approve")
    check(cb is not None and cb() is True and got[-1] == "a", "reply_callback 包装声明过的动作")
    check(R.reply_callback(ev, "confirm") is None, "未在事件 actions 里声明的动作得到 None")
    check(R.reply_callback({"event": "x"}, "approve") is None, "没有 reply_id 的事件得到 None")
    R.discard(rid3)

    # 跨线程回复：回调在调用 resolve 的线程里执行
    seen: list = []
    rid4 = R.register({"confirm": lambda: seen.append(threading.current_thread().name)})
    t = threading.Thread(target=R.resolve, args=(rid4, "confirm"), name="replier")
    t.start(); t.join(5)
    check(seen == ["replier"], "可以从其他线程回复", str(seen))
    R.discard(rid4)


_ON_KEY = re.compile(r"^on_[a-z_]+$")


def _event_dicts(tree: ast.AST):
    """含字符串键 "event" 的字典字面量，以及其中嵌套的字典（如选择卡的 cards）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "event" for k in node.keys):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    yield sub


def t_events_have_no_callables() -> None:
    print("\n▶ 后端事件不含可调用对象")
    files = [f for f in S.module_files("core") if f.suffix == ".py"]
    check(len(files) > 50, "扫描范围是整个 core 包", str(len(files)))
    on_keys: list[str] = []
    lambdas: list[str] = []
    n_events = 0
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        rel = f.relative_to(ROOT).as_posix()
        for d in _event_dicts(tree):
            n_events += 1
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and _ON_KEY.match(k.value):
                    on_keys.append(f"{rel}:{k.lineno} {k.value}")
                if isinstance(v, ast.Lambda):
                    lambdas.append(f"{rel}:{v.lineno}")
    check(n_events > 20, "找到了事件字典", str(n_events))
    check(not on_keys, "事件里没有 on_* 回调键", ", ".join(on_keys[:6]))
    check(not lambdas, "事件里没有 lambda 值", ", ".join(lambdas[:6]))

    # 每个带 reply_id 的事件都声明 actions
    missing: list[str] = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        for d in _event_dicts(tree):
            keys = {k.value for k in d.keys if isinstance(k, ast.Constant)}
            if "reply_id" in keys and "actions" not in keys:
                missing.append(f"{f.name}:{d.lineno}")
    check(not missing, "带 reply_id 的事件都声明了 actions", ", ".join(missing))


def t_ui_does_not_read_callbacks() -> None:
    print("\n▶ 界面侧按 reply_id 回复")
    src = S.module_text("app")
    hits = re.findall(r"""\.get\(\s*["']on_(?:confirm|cancel|always|auto|choice|dismiss|approve|reject)["']""", src)
    check(not hits, "app.py 不再从事件里取 on_* 回调", ", ".join(hits[:5]))
    check(src.count("reply_callback") >= 5, "app.py 用 reply_callback 包装回复", str(src.count("reply_callback")))


def t_wire() -> None:
    print("\n▶ 运行时可序列化检查")
    import asyncio
    from core.runtime import wire as W

    ok_ev = {"event": "x", "a": 1, "b": [1.5, None, True, "s"], "c": {"d": ("t",)}}
    check(W.non_serializable_paths(ok_ev) == [], "JSON 兼容的事件没有问题")

    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    bad_ev = {"event": "y", "task": fut, "cards": [{"f": print}], "m": {1: "k"},
              "p": pathlib.Path("x")}
    bad = W.non_serializable_paths(bad_ev)
    loop.close()
    want = {"task: Future", "cards[0].f: builtin_function_or_method", "m.1: key int"}
    check(want <= set(bad) and any(b.startswith("p: ") and "Path" in b for b in bad) and len(bad) == 4,
          "定位到 Future / 函数 / 非字符串键 / Path", str(bad))

    msgs: list[str] = []
    from loguru import logger
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        W.warn_if_not_serializable({"event": "z_unique", "f": print})
        W.warn_if_not_serializable({"event": "z_unique", "f": print})
    finally:
        logger.remove(hid)
    hits = [m for m in msgs if "z_unique" in m]
    check(len(hits) == 1, "同一事件同一位置只记一次 WARNING", str(len(hits)))

    src = S.module_text("app")
    check(src.count("_wire_check(") >= 3, "界面的两个事件入口都做了检查（定义 + 2 处调用）",
          str(src.count("_wire_check(")))


if __name__ == "__main__":
    print("=" * 74)
    print("确认回复登记表与事件可序列化")
    print("=" * 74)
    t_registry()
    t_events_have_no_callables()
    t_ui_does_not_read_callbacks()
    t_wire()

    _ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    print(f"结果：{_ok}/{len(_results)} 通过")
    print("=" * 74)
    for ok, name, note in _results:
        if not ok:
            print(f"  FAIL  {name}" + (f"   [{note}]" if note else ""))
    sys.exit(0 if _ok == len(_results) else 1)
