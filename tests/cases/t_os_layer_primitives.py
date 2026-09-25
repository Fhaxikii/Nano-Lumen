# -*- coding: utf-8 -*-
"""OS 层两个原语：审计脱敏 + 视觉定位的 JSON 解析。

═══ 为什么单开一个文件 ═══

这两块是 OS 层里仅有的、其余测试完全没有覆盖到的地方 —— 别的文件各有主题
（能力开关、命令分级、窗口绑定…），它俩哪个都不属于，硬塞进去反而看不出在测什么。

📌 顺带一条教训：**一个不会被运行的测试，和它守的性质从未被守过是同一件事。**
   这两块的断言早先存在过，但放在一个没有任何调用方、也不在测试目录里的文件中，
   于是从未真正执行过 —— 它一直"有测试"，而那测试一次都没跑。

═══ 这里守什么 ═══

  ① **审计日志会把敏感入参脱敏**。`type_text` 的 `text` 是用户真正敲进
     别人窗口里的东西（密码、口令都可能），审计要留的是「做过什么」，
     不是「敲了什么」。
     📌 一份为了追责而存在的日志，如果自己成了泄密源，它就从资产变成了负债。

  ② **视觉定位解析模型返回的 JSON 时，四种输入都不许炸**。
     模型不保证只吐纯 JSON —— 它会包 markdown 代码块、会在前后说话、
     也可能什么都没给。这一层必须四种都稳。
     📌 解析失败要返回 None（让上层走降级），不是抛异常（让整轮塌掉）。

用法：
  py -3.10 tests\cases\t_os_layer_primitives.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import tests._console  # noqa: F401

from loguru import logger
logger.remove()

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, note: str = "") -> None:
    _results.append((bool(ok), name, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{note}]" if note else ""))


def t_audit_redacts_sensitive_params() -> None:
    print("\n[1] ⭐⭐ 审计日志把敏感入参脱敏")
    from core.os_layer.audit import OSAuditLogger

    al = OSAuditLogger(log_dir=pathlib.Path(tempfile.mkdtemp()))
    al.record(action="get_sysinfo", params={"fields": ["cpu"]}, effective_risk=1,
              result_status="success", result_summary="CPU 12%")
    al.record(action="type_text", params={"text": "secret123"}, effective_risk=3,
              result_status="success")
    tail = al.tail(5)

    check(len(tail) >= 2, "写进去的能读回来", f"{len(tail)} 条")
    _text = str(tail[-1]["params"].get("text", ""))
    check(_text.startswith("<redacted:"),
          "🔴 `type_text` 的 `text` 已脱敏 —— "
          "📌 审计要留的是「做过什么」，不是「敲了什么」；"
          "一份为追责而存在的日志如果自己泄密，它就从资产变成了负债",
          _text)
    check("secret123" not in str(tail[-1]),
          "⚠️ 原文**整条记录里都不出现**（不只是 params 那一格被换掉）")
    check(tail[0]["effective_risk"] == 1,
          "⭐ 记录里带着 `effective_risk` —— 事后要能答「当时按几级放行的」")


def t_vision_json_parse_never_throws() -> None:
    print("\n[2] ⭐⭐ 视觉定位解析模型输出：四种输入都不许炸")
    from core.os_layer.executor_vision import VisionLocator
    _pj = VisionLocator._parse_json

    check(_pj('```json\n{"found": true, "x": 10}\n```') == {"found": True, "x": 10},
          "⭐ 剥掉 markdown 代码块 —— 📌 模型很爱包一层 ```json，"
          "这不是异常输入，是**常态**")
    check(_pj('随便说点 {"found": false} 然后结束') == {"found": False},
          "⭐ 前后有闲话时也能把 JSON 抓出来")
    check(_pj("") is None,
          "⚠️ 空文本返回 **None**，不抛 —— "
          "📌 解析不出来要让上层走降级，抛异常会把整轮拖塌")
    check(_pj("完全不是json") is None,
          "⚠️ 完全不是 JSON 时同样返回 None，而不是半个字典")


def main() -> int:
    t_audit_redacts_sensitive_params()
    t_vision_json_parse_never_throws()

    ok = sum(1 for r in _results if r[0])
    print("")
    print("=" * 74)
    if ok == len(_results):
        print(f"结果：{ok}/{len(_results)} 通过")
    else:
        print(f"结果：{ok}/{len(_results)} 通过 —— 失败项：")
        for good, name, note in _results:
            if not good:
                print(f"  - {name}   [{note}]")
    print("=" * 74)
    return 0 if ok == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
