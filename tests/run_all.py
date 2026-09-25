# -*- coding: utf-8 -*-
"""运行全部测试。

用法：
  py -3.10 tests\\run_all.py            默认：跳过标记为 live 的测试
  py -3.10 tests\\run_all.py --live     连同 live 测试一起跑
  py -3.10 tests\\run_all.py t_f4 t_os  只跑文件名包含这些片段的测试

- 每个 `tests/cases/t_*.py` 在独立进程里运行；判定与原 run_tests.sh 相同：退出码非 0、
  找不到汇总行、汇总里有失败、或「通过数 != 总数」都算失败。
- 文件前 30 行里有 `# nano-test: live` 的是 live 测试（需要真实桌面、真实模型等），默认跳过。
- **数据目录守卫**：运行前后各记一次仓库 `data/` 下所有文件的大小和修改时间，
  有任何变化即判为失败并列出变化的文件。测试应当只写 `tests/_sandbox.py` 创建的临时目录。
  运行时不要同时开着 Nano（它会写 data/，守卫会把它当成测试写入）。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests" / "cases"
DATA = ROOT / "data"
LIVE_MARK = "# nano-test: live"
_SUMMARY = re.compile(r"(\d+) passed, (\d+) failed|(\d+)/(\d+) (?:通过|passed)")


def is_live(path: pathlib.Path) -> bool:
    with path.open(encoding="utf-8", errors="replace") as f:
        return any(LIVE_MARK in next(f, "") for _ in range(30))


def snapshot() -> dict:
    out = {}
    if DATA.exists():
        for p in DATA.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[str(p.relative_to(ROOT))] = (st.st_size, st.st_mtime_ns)
    return out


def verdict(rc: int, out: str) -> tuple[bool, str]:
    m = None
    for m in _SUMMARY.finditer(out):
        pass
    if m is None:
        return False, f"无汇总 rc={rc}"
    if m.group(1) is not None:
        passed, failed = int(m.group(1)), int(m.group(2))
        summary, ok = f"{passed} passed, {failed} failed", failed == 0
    else:
        passed, total = int(m.group(3)), int(m.group(4))
        summary, ok = f"{passed}/{total}", passed == total
    return (ok and rc == 0), (summary if rc == 0 else f"{summary} rc={rc}")


def main(argv: list[str]) -> int:
    live = "--live" in argv
    filters = [a for a in argv if not a.startswith("--")]
    files = sorted(TESTS.glob("t_*.py"))
    if filters:
        files = [f for f in files if any(x in f.name for x in filters)]

    before = snapshot()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("NANO_DATA_DIR", None)
    failed, skipped, ran = [], [], 0
    t_all = time.time()
    for f in files:
        if is_live(f) and not live:
            skipped.append(f.name)
            print(f"SKIP {f.name} (live)")
            continue
        ran += 1
        t0 = time.time()
        p = subprocess.run([sys.executable, str(f)], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok, summary = verdict(p.returncode, (p.stdout or "") + (p.stderr or ""))
        dt = time.time() - t0
        print(f"{'ok  ' if ok else 'FAIL'} {f.name} -> {summary} {dt:.0f}s", flush=True)
        if not ok:
            failed.append(f.name)
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-12:]
            for line in tail:
                print(f"    | {line}")

    after = snapshot()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    print("")
    print(f"ran {ran}, failed {len(failed)}, skipped {len(skipped)} (live), {time.time() - t_all:.0f}s")
    if changed:
        print(f"DATA GUARD FAIL: {len(changed)} file(s) under data/ changed during the run:")
        for k in changed[:40]:
            print(f"    {k}")
    if failed:
        print("failed: " + ", ".join(failed))
    ok = not failed and not changed
    print("OK" if ok else "NOT OK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
