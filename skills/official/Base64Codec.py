# skills_official/Base64Codec.py
"""官方基础 Skill：Base64 与 URL 编解码。协议 v3.2"""

from __future__ import annotations

import base64
import pathlib
from urllib.parse import quote, unquote

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


class Base64Codec(BaseSkill):
    def get_manifest(self):
        return {
            "name": "Base64Codec",
            "description": "Encode or decode Base64 / URL-safe Base64 / percent-encoding. Use when handling tokens, payloads or API parameters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to process. To encode a file instead, use file_path."},
                    "operation": {"type": "string", "description": "One of base64_encode / base64_decode / urlsafe_encode / urlsafe_decode / url_encode / url_decode. Default base64_encode."},
                    "encoding": {"type": "string", "description": "Text encoding. Default utf-8."},
                    "file_path": {"type": "string", "description": "Optional. Read this file and encode it as base64 / URL-safe base64."},
                    "max_output_chars": {"type": "integer", "description": "Max characters to return. Default 20000."},
                },
                "required": [],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="Base64Codec",
            purpose="执行 Base64、URL-safe Base64 与 URL 百分号编解码，返回确定性结果。",
            required_inputs=[],
            optional_inputs=[
                InputDef("text", "string", "待处理文本。"),
                InputDef("operation", "string", "编码/解码操作，默认 base64_encode。"),
                InputDef("encoding", "string", "文本编码，默认 utf-8。"),
                InputDef("file_path", "file_path", "可选；读取文件并编码。"),
                InputDef("max_output_chars", "integer", "最大返回字符数。"),
            ],
            data_output_keys=["operation", "result", "truncated"],
            side_effects=[SideEffect.FILE_READ],
            permission_level=PermissionLevel.READONLY,
            not_responsible_for=["不联网", "不破解或验证 token", "不写入文件"],
            lifecycle=Lifecycle.PERMANENT,
        )

    @staticmethod
    def _normalize_operation(operation: str) -> str:
        op = (operation or "base64_encode").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "encode": "base64_encode", "b64_encode": "base64_encode", "base64": "base64_encode",
            "decode": "base64_decode", "b64_decode": "base64_decode",
            "urlsafe_base64_encode": "urlsafe_encode", "url_safe_encode": "urlsafe_encode",
            "urlsafe_base64_decode": "urlsafe_decode", "url_safe_decode": "urlsafe_decode",
            "percent_encode": "url_encode", "urlencode": "url_encode",
            "percent_decode": "url_decode", "urldecode": "url_decode",
        }
        op = aliases.get(op, op)
        allowed = {"base64_encode", "base64_decode", "urlsafe_encode", "urlsafe_decode", "url_encode", "url_decode"}
        if op not in allowed:
            raise ValueError(f"unsupported operation: {operation}")
        return op

    @staticmethod
    def _with_padding(s: str) -> str:
        clean = "".join((s or "").strip().split())
        return clean + ("=" * ((4 - len(clean) % 4) % 4))

    @staticmethod
    def _clip(result: str, max_output_chars: int) -> tuple[str, bool, int]:
        limit = max(100, min(int(max_output_chars or 20000), 100000))
        n = len(result)
        if n > limit:
            return result[:limit], True, n
        return result, False, n

    @staticmethod
    def _read_file_bytes(file_path: str) -> tuple[bytes, str]:
        p = pathlib.Path(file_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"file does not exist: {file_path}")
        if not p.is_file():
            raise ValueError(f"not a file: {file_path}")
        return p.read_bytes(), str(p)

    async def run(
        self,
        text: str = "",
        operation: str = "base64_encode",
        encoding: str = "utf-8",
        file_path: str = "",
        max_output_chars: int = 20000,
    ) -> SkillResult:
        try:
            op = self._normalize_operation(operation)
            file_mode = bool((file_path or "").strip())
            resolved_path = ""

            if file_mode:
                if op not in {"base64_encode", "urlsafe_encode"}:
                    return SkillResult(False, "编码处理失败：file_path 只支持 base64_encode / urlsafe_encode", {
                        "operation": op, "result": None, "truncated": False
                    })
                raw_bytes, resolved_path = self._read_file_bytes(file_path)
                source_type = "file"
            else:
                if text is None or text == "":
                    return SkillResult(False, "编码处理失败：text 和 file_path 不能都为空", {
                        "operation": op, "result": None, "truncated": False
                    })
                raw_bytes = text.encode(encoding or "utf-8")
                source_type = "text"

            is_text = True
            binary_preview_hex = ""
            if op == "base64_encode":
                result = base64.b64encode(raw_bytes).decode("ascii")
            elif op == "urlsafe_encode":
                result = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
            elif op == "base64_decode":
                decoded = base64.b64decode(self._with_padding(text), validate=True)
                try:
                    result = decoded.decode(encoding or "utf-8")
                except UnicodeDecodeError:
                    is_text = False
                    result = decoded.decode(encoding or "utf-8", errors="replace")
                    binary_preview_hex = decoded[:64].hex()
            elif op == "urlsafe_decode":
                decoded = base64.urlsafe_b64decode(self._with_padding(text))
                try:
                    result = decoded.decode(encoding or "utf-8")
                except UnicodeDecodeError:
                    is_text = False
                    result = decoded.decode(encoding or "utf-8", errors="replace")
                    binary_preview_hex = decoded[:64].hex()
            elif op == "url_encode":
                result = quote(text, safe="")
            elif op == "url_decode":
                result = unquote(text)
            else:
                raise ValueError(f"unsupported operation: {op}")

            clipped, truncated, full_len = self._clip(result, max_output_chars)
            data = {
                "operation": op,
                "result": clipped,
                "truncated": truncated,
                "full_length_chars": full_len,
                "source_type": source_type,
                "is_text": is_text,
            }
            if resolved_path:
                data["file_path"] = resolved_path
            if binary_preview_hex:
                data["binary_preview_hex"] = binary_preview_hex
            suffix = "（已截断）" if truncated else ""
            return SkillResult(True, f"编码处理完成：{op}{suffix}", data)
        except Exception as e:
            return SkillResult(False, f"编码处理失败：{e}", {
                "operation": operation or "base64_encode", "result": None, "truncated": False
            })
