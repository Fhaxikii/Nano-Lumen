# core/registry.py
import importlib.util
import os
import pathlib
import inspect
import traceback
import sys
import threading
import shutil
from typing import Dict, Any, List, Optional
from loguru import logger
from core.schema import BaseSkill


class SkillRegistry:
    def __init__(self):
        self.skills: Dict[str, BaseSkill] = {}
        self.tools_manifest: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        # 官方基础 Skill 名字集合：来自 skills/official/ 目录，
        # 在 UI 上和永久 Skill 没有区别，唯一区别是不可禁用/删除。
        self._official_skill_names: set = set()

    @staticmethod
    def _manifest_name(manifest: Dict[str, Any]) -> Optional[str]:
        """取 manifest 的 name。**只认一种形状：name 在最外层。**

        🪦 2026-08-29 删掉了 Gemini 风格的 `function_declarations` 包壳分支。
           它是 Gemini 时代的遗留，而项目现在只接 Anthropic。

        🔴 更要紧的是它造成的**形状不一致**：同一个文件里，
             这里（解析）**接受** function_declarations 包壳
             下面（校验）对 OpenAI 包壳打 🔴 error
             `Orchestrator._check_manifest_shape` 在审计期**两种都拒**
           ⇒ 一个宽松的解析器贴着一个严格的校验器：
             用 Gemini 包壳写的 Skill 能被读出名字、能进清单，
             却会在审计那一步被拒 —— 而两处给的信号完全相反。
           📌 **一个东西合不合法，只能有一处说了算。**
              解析器比校验器宽松，等于给出一条「先通过、后被拒」的路，
              而走这条路的人会以为自己写对了。
        """
        if not isinstance(manifest, dict):
            return None
        return manifest.get("name")

    def _append_manifest_dedup(self, manifests: List[Dict[str, Any]], manifest: Dict[str, Any]):
        """按 function/name 去重，避免 Gemini Duplicate function declaration。"""
        if not isinstance(manifest, dict):
            return
        # ⚠️ 不再拆 `function_declarations` 包壳（见 `_manifest_name` 的留痕）：
        #    校验那边两种包壳都拒，解析这边就不该独自放行。
        items = [manifest]
        for item in items or []:
            name = self._manifest_name(item)
            if not name:
                # ⚠️ 这不是"跳过一个小问题"，而是**这个 Skill 对模型彻底不存在**：
                # 文件在、装载成功、UI 显示 READY，但工具清单里没有它。
                # 原来这行只是 `manifest 缺少 name，已跳过` —— 读日志的人看不出后果有多大。
                _looks_openai = isinstance(item, dict) and "function" in item and "type" in item
                logger.error(
                    "🔴 [Skill] manifest 顶层没有 name，**整条被丢弃 → 模型看不见这个 Skill**"
                    "（文件仍在、UI 仍显示 READY，但它调不到）。"
                    + ("原因：用了 OpenAI 风格的嵌套外壳 {\"type\":\"function\",\"function\":{…}}，"
                       "name/description/parameters 必须放在最外层。" if _looks_openai else "")
                    + f" 顶层键: {sorted(item.keys()) if isinstance(item, dict) else type(item).__name__}。"
                    " 修法：让 Nano 重新生成这个 Skill（校验器现在会在审计窗口拦住这种形状）。"
                )
                continue
            old_index = next((i for i, m in enumerate(manifests) if self._manifest_name(m) == name), None)
            if old_index is not None:
                logger.warning(f"⚠️ 重复 Skill manifest: {name}，保留后加载版本")
                manifests[old_index] = item
            else:
                manifests.append(item)

            # 🪦 `scan_skills` 已删除（2026-08-29）—— **零调用方**，AST 全仓核实。
            #    📌 一个写好但没人调的东西，比没写更坏：没写时缺口是可见的，
            #       写了不接时缺口看起来已经补上了。

    def _load_skill_file(self, file_path: pathlib.Path, next_skills: Dict[str, BaseSkill], next_manifests: List[Dict[str, Any]]) -> set:
        """加载单个 Skill 文件，把发现的类注册进 next_skills/next_manifests。

        返回值：本次调用里【注册或覆盖】的 instance.name 集合。
        reload_all() 用它来判断"这个文件刚刚贡献了哪些名字"（官方 Skill
        撞名覆盖时据此标记 _official_skill_names），比"调用前后
        next_skills.keys() 的差集"可靠——差集在撞名覆盖场景下会算出空集。
        """
        module_name = f"_nano_skill_{file_path.stem}"
        registered_names: set = set()
        try:
            if module_name in sys.modules:
                del sys.modules[module_name]

            spec = importlib.util.spec_from_file_location(module_name, str(file_path))
            if spec is None or spec.loader is None:
                logger.warning(f"⚠️ 无法创建模块 spec: {file_path.name}")
                return registered_names
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            found = False
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if not (issubclass(obj, BaseSkill) and obj is not BaseSkill):
                    continue
                if obj.__module__ != module_name:
                    continue

                instance = obj()
                if instance.name in next_skills:
                    logger.warning(f"⚠️ 重复 Skill 名称: {instance.name}，保留后加载版本")
                next_skills[instance.name] = instance
                registered_names.add(instance.name)

                try:
                    manifest = instance.get_manifest()
                except (NotImplementedError, AttributeError):
                    run_method = getattr(instance, "run", None)
                    doc = run_method.__doc__ if run_method else ""
                    manifest = {
                        "name": instance.name,
                        "description": doc.strip().split("\n")[0] if doc else "Local mounted Skill",
                        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
                    }

                self._append_manifest_dedup(next_manifests, manifest)
                logger.debug(f"[Skills] 装载: {instance.name} ({file_path.name})")
                found = True

            if not found:
                logger.warning(f"⚠️ {file_path.name} 中未找到合法的 BaseSkill 子类")

        except Exception as e:
            logger.error(f"❌ 模块 {file_path.name} 加载异常: {e}\n{traceback.format_exc()}")

        return registered_names


    def reload_all(self):
        with self._lock:
            next_skills: Dict[str, BaseSkill] = {}
            next_manifests: List[Dict[str, Any]] = []

            # 扫持久 Skill
            root_path = self._root_path()
            for folder in ["skills"]:
                skills_dir = root_path / folder
                if not skills_dir.exists():
                    continue
                for file_path in skills_dir.glob("*.py"):
                    if file_path.name.startswith("_"):
                        continue
                    self._load_skill_file(file_path, next_skills, next_manifests)

            # 扫官方基础 Skill(skills/official/)——最后加载，撞名时优先级
            # 最高（覆盖 skills/ 里的同名实例）。
            next_official_names: set = set()
            official_dir = self._official_dir()
            if official_dir.exists():
                for file_path in official_dir.glob("*.py"):
                    if file_path.name.startswith("_"):
                        continue
                    registered = self._load_skill_file(file_path, next_skills, next_manifests)
                    next_official_names.update(registered)

            self.skills = next_skills
            self.tools_manifest = next_manifests
            self._official_skill_names = next_official_names
            logger.info(f"[Skills] 已加载 {len(self.skills)} 个技能")
            logger.debug(
                f"[Skills] 已注册: {list(self.skills.keys())}"
                + (f" [官方: {sorted(next_official_names)}]" if next_official_names else "")
            )

    def get_all_manifests(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.tools_manifest)

    def get_manifest(self, skill_name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for m in self.tools_manifest:
                if self._manifest_name(m) == skill_name:
                    return m
        return None

    def is_os_skill(self, skill_name: str) -> bool:
        """这个 Skill 是不是 OS Skill——纯展示用的判断，不用于任何安全/
        权限逻辑。side_effects 活在 get_spec()（SkillSpec），不在
        get_manifest() 里，所以不能走 tools_manifest，得直接查
        self.skills 这个实例字典。旧协议 Skill 没实现 get_spec()，
        NotImplementedError 当作"不是 OS Skill"处理。
        side_effects 是模型生成代码时自己声明的，部署时有 AST 一致性
        校验，但运行时没有二次验证——纯展示场景下可信，不要拿这个判断
        结果做权限相关的事。"""
        with self._lock:
            skill_obj = self.skills.get(skill_name)
        if skill_obj is None:
            return False
        try:
            spec = skill_obj.get_spec()
        except (NotImplementedError, AttributeError):
            return False
        return "os_control" in (getattr(spec, "side_effects", None) or [])

    def is_official_skill(self, skill_name: str) -> bool:
        """是不是 skills/official/ 里的预置基础 Skill（公开包一层，UI 侧
        不用直接伸手进 _official_skill_names 这个内部集合）。"""
        with self._lock:
            return skill_name in self._official_skill_names

    # ⚠️ 这里曾有 `get_skill_awareness_list()`（给能力边界块产 Skill 分类列表），
    #    切换完成时删除。直接理由是它里面那行对 description 做的
    #    **50 字符截断** —— 那是「拿截断冒充语义摘要」的**另一半**：
    #    `_build_deferred_awareness` 的 28 字符截断修了、这一处留着，问题就没死透。
    #    两处是同一个问题：**拿字符串截断冒充语义摘要**。
    #    ⭐ 现在 Skill 的一句话感知由统一工具目录投影（按词/句边界压缩），
    #       而「官方 / 用户」这个分类仍然问 `is_official_skill()` ——
    #       那是 registry 的原生事实，不该塞进 ToolDefinition。


    def get_all_callables(self) -> List[Any]:
        with self._lock:
            skill_values = list(self.skills.values())
        callables = []
        for instance in skill_values:
            func_target = getattr(instance, instance.name, getattr(instance, "run", None))
            if func_target and (inspect.ismethod(func_target) or inspect.isfunction(func_target)):
                callables.append(func_target)
        return callables

    def validate_params(self, skill_name: str, params: Dict[str, Any]) -> tuple[bool, str]:
        manifest = self.get_manifest(skill_name)
        if not manifest:
            return False, f"Skill manifest not found: {skill_name}"
        required = ((manifest.get("parameters") or {}).get("required") or [])
        missing = [k for k in required if k not in (params or {})]
        if missing:
            return False, f"Missing required parameter(s): {', '.join(missing)}"
        return True, "OK"

    def _validate_file_path_params(self, skill, params: Dict[str, Any]) -> Optional[str]:
        """校验 InputDef.type == "file_path" 的参数是否为真实存在的绝对路径。

        背景：模型经常跳过 get_file_path、直接把裸文件名（如'客户消费记录.xlsx'）
        当 file_path 参数传进来，之前完全靠模型自觉，没有任何代码兜底，
        导致 Skill 内部用这个假路径调用 pandas/open() 时才报 FileNotFoundError——
        报错信息对模型不够直接，排查链路绕了一圈。这里提前拦截，给出
        明确指引（先调 get_file_path），比让 Skill 自己摔出文件系统异常更快定位。
        """
        try:
            spec = skill.get_spec()
        except Exception:
            return None
        path_param_names = {
            inp.name for inp in (spec.required_inputs or [])
            if getattr(inp, "type", None) == "file_path"
        }
        if not path_param_names:
            return None
        for name in path_param_names:
            value = (params or {}).get(name)
            if not isinstance(value, str) or not value:
                continue
            if not os.path.isabs(value) or not os.path.exists(value):
                return (
                    f"Parameter \"{name}\" value \"{value}\" is not an existing absolute disk path. "
                    f"Call get_file_path first to resolve the Nano filename into a real path, "
                    f"then pass the returned absolute path to this Skill's \"{name}\" parameter. "
                    f"Do not pass the filename directly."
                )
        return None

    async def execute(self, skill_name: str, params: Dict[str, Any],
                      progress_ref: str = ""):
        with self._lock:
            skill = self.skills.get(skill_name)
        if skill is None:
            logger.warning(f"[Registry] 未找到技能: {skill_name}")
            return None
        ok, msg = self.validate_params(skill_name, params or {})
        if not ok:
            # ⚠️ 诊断：只在失败时打。参数校验失败时**必须能看到它到底传了什么** ——
            #    否则「缺 query」和「传了个空 dict」在日志上长得一模一样。
            logger.warning(
                f"[Registry-Diag] {skill_name} 参数校验失败: {msg} | "
                f"keys={sorted((params or {}).keys())} | raw={str(params)[:300]}"
            )
            return f"Execution failed: {msg}"
        path_err = self._validate_file_path_params(skill, params)
        if path_err:
            return f"Execution failed: {path_err}"
        try:
            # 净化 format 类参数，防止模型传字面量（如 "HH:mm"）
            from core.schema import BaseSkill as _BS
            cleaned_params = _BS.sanitize_format_arg(params or {})
            # ⭐ 把「本次调用的进度 ref」绑到执行上下文 —— Skill 里
            #    `self.report_progress(...)` 就是往这个 ref 上报。
            # ⚠️ 绑在**上下文**而不是 `skill` 实例上：`self.skills` 存的是单例，
            #    并发两次调用会互相覆盖实例字段。
            #    📌 一个单例上的「本次调用」状态，在并发下必然串味。
            # ⚠️ `finally` 里必须 unbind：不还原的话这个 ref 会漏给同一个
            #    上下文里后续的调用，于是**下一个 Skill 的进度写进上一个的轨迹**。
            #    📌 一个「本次有效」的绑定，必须在每一条离开的路径上被还原。
            _tok = None
            try:
                from core.runtime import progress as _pb
                _tok = _pb.bind(progress_ref)
            except Exception:
                _pb = None
            try:
                return await skill.run(**cleaned_params)
            finally:
                if _pb is not None and _tok is not None:
                    _pb.unbind(_tok)
        except TypeError as e:
            logger.error(f"[Registry] 技能 {skill_name} 参数错误: {e}")
            return f"Execution failed: parameter error: {e}"


    # ── Skill 生命周期管理 ───────────────────────────────────────────────

    def _root_path(self) -> pathlib.Path:
        from core.paths import ROOT
        return ROOT

    def _skills_dir(self) -> pathlib.Path:
        path = self._root_path() / "skills"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _disabled_dir(self) -> pathlib.Path:
        path = self._root_path() / "skills" / "disabled"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _official_dir(self) -> pathlib.Path:
        """官方基础 Skill 目录(skills/official/)。和 skills/ 里的永久 Skill 在 UI 上
        没有区别，唯一区别是不可被禁用/删除——这里的内容由维护者直接管理（手动
        增删文件），不通过 UI 操作。"""
        path = self._root_path() / "skills" / "official"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_permanent_manifests(self) -> List[Dict[str, Any]]:
        """返回全部 Skill manifest,供普通工具清单使用。
        （临时 Skill 概念已废弃，所有 Skill 都是常驻，等价于 get_all_manifests。）
        """
        with self._lock:
            return list(self.tools_manifest)

    def list_enabled_skills(self) -> List[str]:
        with self._lock:
            return sorted(self.skills.keys())

    def list_disabled_skills(self) -> List[str]:
        disabled_dir = self._disabled_dir()
        names = []
        for path in disabled_dir.glob("*.py"):
            if not path.name.startswith("_"):
                names.append(path.stem)
        return sorted(names)

    def list_deleted_skills(self, limit: int = 30) -> List[str]:
        """供分类器 target_skill 归一化使用：删除时若撞名会加时间戳后缀
        （见 delete_skill_file），这里要去掉后缀还原成原始 skill 名，
        否则分类器永远匹配不上用户打出来的原名。按最近删除时间取前
        limit 个，避免这个目录积累多年后把分类器 prompt 撑爆。"""
        import re as _re
        deleted_dir = self._root_path() / "skills" / "deleted"
        if not deleted_dir.exists():
            return []
        files = [p for p in deleted_dir.glob("*.py") if not p.name.startswith("_")]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        names: List[str] = []
        for path in files[:limit]:
            stem = _re.sub(r"_\d{10,}$", "", path.stem)
            if stem not in names:
                names.append(stem)
        return names

    def get_skill_path(self, skill_name: str, include_disabled: bool = False) -> Optional[pathlib.Path]:
        """返回指定 Skill 名字对应的源文件路径。

        官方基础 Skill(skills/official/) 在 reload_all() 里最后加载、
        撞名优先级最高，所以这里也排第一位——保证"查到的路径"和
        "实际在跑的代码"一致。
        """
        if not skill_name:
            return None
        official = self._official_dir() / f"{skill_name}.py"
        if official.exists():
            return official
        enabled = self._skills_dir() / f"{skill_name}.py"
        if enabled.exists():
            return enabled
        if include_disabled:
            disabled = self._disabled_dir() / f"{skill_name}.py"
            if disabled.exists():
                return disabled
        return None

    def get_skill_source(self, skill_name: str, include_disabled: bool = False) -> Optional[Dict[str, Any]]:
        path = self.get_skill_path(skill_name, include_disabled=include_disabled)
        if not path:
            return None
        try:
            code = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            code = path.read_text(encoding="utf-8", errors="ignore")
        return {
            "skill_name": skill_name,
            "filename": path.name,
            "path": str(path),
            "disabled": path.parent.name == "disabled",
            "manifest": None if path.parent.name == "disabled" else self.get_manifest(skill_name),
            "code": code,
        }

    def update_skill_file(self, skill_name: str, code: str) -> Dict[str, Any]:
        if not skill_name:
            return {"ok": False, "msg": "Skill name is empty"}
        if skill_name in self._official_skill_names:
            return {"ok": False, "msg": f"\"{skill_name}\" is an official built-in Skill and cannot be modified directly"}
        path = self.get_skill_path(skill_name, include_disabled=False)
        if not path:
            return {"ok": False, "msg": f"Enabled Skill not found: {skill_name}"}
        try:
            backup_dir = self._root_path() / "skills" / "backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{skill_name}.py.bak"
            shutil.copy2(path, backup_path)
            path.write_text(code, encoding="utf-8")
            self.reload_all()
            return {"ok": True, "msg": f"Skill \"{skill_name}\" has been overwritten, updated, and hot-loaded", "backup": str(backup_path)}
        except Exception as e:
            logger.error(f"[Registry] 更新 Skill 失败 {skill_name}: {e}")
            return {"ok": False, "msg": str(e)}

    def delete_skill_file(self, skill_name: str, include_disabled: bool = True) -> Dict[str, Any]:
        if skill_name in self._official_skill_names:
            return {"ok": False, "msg": f"\"{skill_name}\" is an official built-in Skill and cannot be deleted"}
        path = self.get_skill_path(skill_name, include_disabled=include_disabled)
        if not path:
            return {"ok": False, "msg": f"Skill not found: {skill_name}"}
        try:
            backup_dir = self._root_path() / "skills" / "deleted"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / path.name
            if backup_path.exists():
                backup_path = backup_dir / f"{path.stem}_{int(__import__('time').time())}.py"
            shutil.move(str(path), str(backup_path))
            self.reload_all()
            return {"ok": True, "msg": f"Skill \"{skill_name}\" has been deleted and backed up to {backup_path}", "backup": str(backup_path)}
        except Exception as e:
            logger.error(f"[Registry] 删除 Skill 失败 {skill_name}: {e}")
            return {"ok": False, "msg": str(e)}

    def disable_skill(self, skill_name: str) -> Dict[str, Any]:
        if skill_name in self._official_skill_names:
            return {"ok": False, "msg": f"\"{skill_name}\" is an official built-in Skill and cannot be disabled"}
        path = self.get_skill_path(skill_name, include_disabled=False)
        if not path:
            return {"ok": False, "msg": f"Enabled Skill not found: {skill_name}"}
        try:
            target = self._disabled_dir() / path.name
            if target.exists():
                target = self._disabled_dir() / f"{path.stem}_{int(__import__('time').time())}.py"
            shutil.move(str(path), str(target))
            self.reload_all()
            return {"ok": True, "msg": f"Skill \"{skill_name}\" has been disabled", "path": str(target)}
        except Exception as e:
            logger.error(f"[Registry] 禁用 Skill 失败 {skill_name}: {e}")
            return {"ok": False, "msg": str(e)}

    def enable_skill(self, skill_name: str) -> Dict[str, Any]:
        disabled = self._disabled_dir() / f"{skill_name}.py"
        if not disabled.exists():
            return {"ok": False, "msg": f"Disabled Skill not found: {skill_name}"}
        try:
            target = self._skills_dir() / disabled.name
            if target.exists():
                return {"ok": False, "msg": f"Enable failed: an enabled Skill with the same name already exists: \"{skill_name}\""}
            shutil.move(str(disabled), str(target))
            self.reload_all()
            return {"ok": True, "msg": f"Skill \"{skill_name}\" has been enabled", "path": str(target)}
        except Exception as e:
            logger.error(f"[Registry] 启用 Skill 失败 {skill_name}: {e}")
            return {"ok": False, "msg": str(e)}


registry = SkillRegistry()
