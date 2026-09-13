#!/usr/bin/env bash
bad=0
for f in tests/t_*.py; do
  out=$(PYTHONIOENCODING=utf-8 python "$f" 2>&1); rc=$?
  s=$(echo "$out" | grep -oE "[0-9]+ passed, [0-9]+ failed|[0-9]+/[0-9]+ (通过|passed)" | tail -1)
  p=$(echo "$s" | grep -oE "^[0-9]+/[0-9]+" | cut -d/ -f1)
  t=$(echo "$s" | grep -oE "^[0-9]+/[0-9]+" | cut -d/ -f2)
  nf=$(echo "$s" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+")
  if [ "$rc" != "0" ] || [ -z "$s" ] \
     || { [ -n "$nf" ] && [ "$nf" -gt 0 ]; } \
     || { [ -n "$p" ] && [ "$p" != "$t" ]; }; then echo "FAIL $f -> ${s:-无汇总} rc=$rc"; bad=1; fi
done
[ "$bad" = 0 ] && echo "OK 真·全量 0 失败"
