# skills_official/HashGenerator.py
"""官方基础 Skill：文本/文件哈希与 HMAC。协议 v3.2"""

from __future__ import annotations

import hashlib
import hmac
import pathlib
from typing import Dict

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


class HashGenerator(BaseSkill):
    _ALGORITHMS = {
        "md5", "sha1", "sha224", "sha256", "sha384", "sha512",
        "sha3_224", "sha3_256", "sha3_384", "sha3_512", "blake2b", "blake2s",
    }

    def get_manifest(self):
        return {
            "name": "HashGenerator",
            "description": "Compute MD5 / SHA1 / SHA256 / SHA512 of a string or a file. Use to verify a digest or check file integrity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to hash. Provide either text or file_path, not both."},
                    "file_path": {"type": "string", "description": "Local file to hash. Provide either text or file_path, not both."},
                    "algorithm": {"type": "string", "description": "Algorithm: md5 / sha1 / sha256 / sha512 / sha3_256 / blake2b and similar. Default sha256."},
                    "encoding": {"type": "string", "description": "Text encoding. Default utf-8."},
                    "hmac_key": {"type": "string", "description": "Optional. If given, compute an HMAC digest instead. The key itself is never returned."},
                    "uppercase": {"type": "boolean", "description": "Return the digest in uppercase. Default false."},
                },
                "required": [],
            },
        }

    def get_spec(self) -> SkillSpec:
        return SkillSpec(
            name="HashGenerator",
            purpose="计算文本或本地文件的哈希/HMAC 摘要，返回确定性 digest。",
            required_inputs=[],
            optional_inputs=[
                InputDef("text", "string", "文本；与 file_path 二选一。"),
                InputDef("file_path", "file_path", "本地文件路径；与 text 二选一。"),
                InputDef("algorithm", "string", "摘要算法，默认 sha256。"),
                InputDef("encoding", "string", "文本编码，默认 utf-8。"),
                InputDef("hmac_key", "string", "可选 HMAC 密钥。"),
                InputDef("uppercase", "boolean", "是否大写输出。"),
            ],
            data_output_keys=["algorithm", "digest", "source_type"],
            side_effects=[SideEffect.FILE_READ],
            permission_level=PermissionLevel.READONLY,
            not_responsible_for=["不联网", "不破解哈希", "不保存 HMAC 密钥"],
            lifecycle=Lifecycle.PERMANENT,
        )

    @classmethod
    def _normalize_algorithm(cls, algorithm: str) -> str:
        alg = (algorithm or "sha256").strip().lower().replace("-", "_")
        aliases = {
            "sha_1": "sha1", "sha_224": "sha224", "sha_256": "sha256",
            "sha_384": "sha384", "sha_512": "sha512", "sha256sum": "sha256",
        }
        alg = aliases.get(alg, alg)
        if alg not in cls._ALGORITHMS:
            raise ValueError(f"unsupported algorithm: {algorithm}")
        return alg

    @staticmethod
    def _new_hash(algorithm: str):
        return hashlib.new(algorithm)

    def _hash_text(self, text: str, algorithm: str, encoding: str, hmac_key: str) -> tuple[str, int]:
        raw = (text or "").encode(encoding or "utf-8")
        if hmac_key:
            digest = hmac.new(hmac_key.encode(encoding or "utf-8"), raw, algorithm).hexdigest()
        else:
            h = self._new_hash(algorithm)
            h.update(raw)
            digest = h.hexdigest()
        return digest, len(raw)

    def _hash_file(self, file_path: str, algorithm: str, hmac_key: str, encoding: str) -> tuple[str, int, str]:
        p = pathlib.Path(file_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"file does not exist: {file_path}")
        if not p.is_file():
            raise ValueError(f"not a file: {file_path}")
        if hmac_key:
            h = hmac.new(hmac_key.encode(encoding or "utf-8"), digestmod=algorithm)
        else:
            h = self._new_hash(algorithm)
        total = 0
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                total += len(chunk)
                h.update(chunk)
        return h.hexdigest(), total, str(p)

    async def run(
        self,
        text: str = "",
        file_path: str = "",
        algorithm: str = "sha256",
        encoding: str = "utf-8",
        hmac_key: str = "",
        uppercase: bool = False,
    ) -> SkillResult:
        try:
            alg = self._normalize_algorithm(algorithm)
            source_type = "file" if (file_path or "").strip() else "text"
            if source_type == "file":
                digest, byte_count, resolved_path = self._hash_file(file_path, alg, hmac_key or "", encoding)
                data: Dict[str, object] = {
                    "algorithm": alg,
                    "digest": digest.upper() if uppercase else digest,
                    "source_type": "file",
                    "byte_count": byte_count,
                    "file_path": resolved_path,
                    "hmac": bool(hmac_key),
                }
            else:
                if text is None or text == "":
                    return SkillResult(False, "哈希计算失败：text 和 file_path 不能都为空", {
                        "algorithm": alg, "digest": None, "source_type": "none"
                    })
                digest, byte_count = self._hash_text(text, alg, encoding, hmac_key or "")
                data = {
                    "algorithm": alg,
                    "digest": digest.upper() if uppercase else digest,
                    "source_type": "text",
                    "byte_count": byte_count,
                    "hmac": bool(hmac_key),
                }
            label = "HMAC" if data.get("hmac") else "Hash"
            return SkillResult(True, f"{label} 结果：{data['digest']}", data)
        except Exception as e:
            return SkillResult(False, f"哈希计算失败：{e}", {
                "algorithm": algorithm or "sha256", "digest": None, "source_type": "file" if file_path else "text"
            })
