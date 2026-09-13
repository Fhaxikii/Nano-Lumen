# skills_official/RegexTester.py
"""官方基础 Skill：正则测试、匹配、替换预览。协议 v3.2"""

from __future__ import annotations

import re

from core.schema import (
    BaseSkill,
    SkillResult,
    SkillSpec,
    InputDef,
    ContextLevel,
    SideEffect,
    PermissionLevel,
    Lifecycle,
)


_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
    "a": re.ASCII,
}


class RegexTester(BaseSkill):
    def get_manifest(self):
        return {
            "name": "RegexTester",
            "description": "Test a Python regular expression and get back matches, groups and positions. Use to check whether a pattern matches before relying on it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "The regular expression to test."},
                    "text": {"type": "string", "description": "The text to match against."},
                    "flags": {"type": "string", "description": "Optional flags: any of i/m/s/x/a, e.g. \"im\"."},
                    "replacement": {"type": "string", "description": "Optional replacement text. If given, a preview of the substitution is returned as well."},
                    "max_matches": {"type": "integer", "description": "Max number of matches to return. Default 20."},
                },
                "required": ["pattern", "text"],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="RegexTester",
            purpose="用 Python re 测试正则，返回匹配、分组、位置和可选替换预览。",
            required_inputs=[
                InputDef("pattern", "string", "正则表达式。"),
                InputDef("text", "string", "待匹配文本。"),
            ],
            optional_inputs=[
                InputDef("flags", "string", "i/m/s/x/a 组合。"),
                InputDef("replacement", "string", "可选替换文本。"),
                InputDef("max_matches", "integer", "最多返回匹配数。"),
            ],
            data_output_keys=["matched", "count", "matches"],
            side_effects=[SideEffect.NONE],
            permission_level=PermissionLevel.READONLY,
            not_responsible_for=["不保证跨语言正则兼容", "不执行任意代码", "不修改原文"],
            lifecycle=Lifecycle.PERMANENT,
        )

    @staticmethod
    def _parse_flags(flags: str) -> tuple[int, list[str]]:
        value = 0
        used: list[str] = []
        for ch in (flags or "").lower():
            if ch in _FLAG_MAP and ch not in used:
                value |= _FLAG_MAP[ch]
                used.append(ch)
        return value, used

    async def run(self, pattern: str, text: str, flags: str = "", replacement: str = "", max_matches: int = 20) -> SkillResult:
        try:
            if not pattern:
                return SkillResult(False, "正则测试失败：pattern 不能为空", {"matched": False, "count": 0, "matches": []})
            max_matches = max(1, min(int(max_matches or 20), 100))
            flag_value, flag_names = self._parse_flags(flags)
            rx = re.compile(pattern, flag_value)
            matches = []
            total = 0
            truncated = False
            for m in rx.finditer(text or ""):
                total += 1
                if len(matches) >= max_matches:
                    truncated = True
                    continue
                item = {
                    "match": m.group(0),
                    "span": [m.start(), m.end()],
                    "groups": list(m.groups()),
                    "groupdict": m.groupdict(),
                }
                matches.append(item)
            data = {
                "matched": total > 0,
                "count": total,
                "matches": matches,
                "truncated": truncated,
                "flags": flag_names,
            }
            if replacement != "":
                replaced, n = rx.subn(replacement, text or "")
                data["replacement"] = replacement
                data["replace_count"] = n
                data["replaced_text"] = replaced
            return SkillResult(
                True,
                f"正则匹配：{total} 处" + ("（结果已截断）" if truncated else ""),
                data,
            )
        except re.error as e:
            return SkillResult(
                False,
                f"正则语法错误：{e}",
                {"matched": False, "count": 0, "matches": [], "error": str(e)},
            )
        except Exception as e:
            return SkillResult(False, f"正则测试失败：{e}", {"matched": False, "count": 0, "matches": []})
