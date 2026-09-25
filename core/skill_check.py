# core/skill_check.py
"""Skill 代码的协议校验：审计窗口展示前、部署前都调这里。

纯函数，只看代码文本（与 AST），不依赖任何运行时状态。副作用扫描本身在
`core/code_scan.py`（临时执行通道也用它，判据只有一处）。
"""
from __future__ import annotations

from loguru import logger

from core.code_scan import detect_side_effects


def _reserved_tool_names() -> set[str]:
    """本地 Skill 不能占用的名字 = 全部内置工具名。

    工具目录按名字登记，Skill 与内置工具重名时 Skill 会被隔离（模型看不见它），
    而 UI 上它照样显示已安装。所以要在审计时就拦下，名单从内置工具清单派生，
    不手抄（手抄的名单曾只覆盖 40 个内置工具中的 11 个）。
    """
    from core.tools.manifests import BUILTIN_MANIFESTS
    return set(BUILTIN_MANIFESTS)


def _manifest_name_literal(tree) -> str | None:
    """`get_manifest` 返回的字典字面量里顶层 `name` 的值；看不清（间接返回等）时为 None。"""
    import ast as _ast
    for fn in _ast.walk(tree):
        if isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and fn.name == "get_manifest":
            for n in _ast.walk(fn):
                if isinstance(n, _ast.Return) and isinstance(n.value, _ast.Dict):
                    for k, v in zip(n.value.keys, n.value.values):
                        if isinstance(k, _ast.Constant) and k.value == "name" \
                                and isinstance(v, _ast.Constant) and isinstance(v.value, str):
                            return v.value
            return None
    return None


def validate_skill_code(code: str, spec_side_effects: list | None = None) -> tuple[bool, list[str]]:
    """代码协议校验。

    新增参数 spec_side_effects:
      如果传入 SkillSpec 声明的 side_effects,额外做 AST 级一致性校验
      (防止模型声明 readonly 但代码里偷偷 file_write)。
      不传则只做协议格式校验(向后兼容)。
    """
    import ast
    errors = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"语法错误: {e}"]

    # ── 基础导入校验(v3.2 要求导入 SkillResult) ──────────────────────
    if "from core.schema import" not in code:
        errors.append("缺少 'from core.schema import ...' 导入")
    elif "BaseSkill" not in code:
        errors.append("未导入 BaseSkill")

    if not any(isinstance(n, ast.ClassDef) for n in ast.walk(tree)):
        errors.append("缺少类定义")
    if "def get_manifest" not in code:
        errors.append("缺少 get_manifest() 方法")
    else:
        # ⭐ manifest 的**外层形状**必须能被 registry 认出来（2026-08-05 实测）
        #
        # 实测现象：`ExtractIP` 部署成功、热载成功、UI 显示 READY，
        # 但加载日志里一行 `⚠️ manifest 缺少 name，已跳过`，
        # **manifest 被整条丢弃 → 模型的工具清单里根本没有这个 Skill。**
        # 用户看到它在列表里、状态 READY，Nano 却用不了它 —— 没有任何人被告知。
        #
        # 根因：模型返回了 OpenAI 风格的嵌套外壳
        #   {"type": "function", "function": {"name": ...}}
        # 而 `registry._manifest_name()` 取的是**顶层** `name`。
        # 协议把内层字段（3.1 参数对应、3.2 类型小写）规定得极细，
        # **却从来没规定过骨架** —— 规定了细节、没规定形状。
        #
        # 这里按 registry 的同一判据静态核一次：取不到名字就在**审计窗口**拦住，
        # 而不是等到加载期只留一行 WARNING。
        _m_err = check_manifest_shape(tree)
        if _m_err:
            errors.append(_m_err)
    if "def get_spec" not in code:
        errors.append("缺少 get_spec() 方法(v3.2 协议要求)")
    if "async def run" not in code:
        errors.append("缺少 async def run 方法")

    # ── SkillResult 返回校验(v3.2 要求返回 SkillResult 而非 str) ──────
    if "SkillResult" not in code:
        errors.append("run() 必须返回 SkillResult 而非字符串(v3.2 协议要求)")

    # ── run() docstring 校验 ─────────────────────────────────────────
    has_run_docstring = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            if (node.body and isinstance(node.body[0], ast.Expr) and
                isinstance(node.body[0].value, ast.Constant) and
                isinstance(node.body[0].value.value, str)):
                has_run_docstring = True
            break
    if not has_run_docstring:
        errors.append("run() 方法缺少 docstring")

    # ── 类名 / self.name 一致性 ──────────────────────────────────────
    # ── 类名 / self.name 一致性 ──────────────────────────────────────
    # 修正:BaseSkill.__init__ 已自动设 self.name = self.__class__.__name__。
    # 子类如果没有自定义 __init__,self.name 自动等于类名,无需显式赋值。
    # 只有子类有自定义 __init__ 时,才检查显式赋值是否与类名一致。
    class_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    # 找出有自定义 __init__ 的类
    classes_with_init = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name not in ("BaseSkill",):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    classes_with_init.add(node.name)
                    break
    for cls_name in class_names:
        if cls_name in ("BaseSkill",):
            continue
        if cls_name in classes_with_init:
            # 有自定义 __init__:必须显式赋值且与类名一致
            pattern = f'self.name = "{cls_name}"'
            if pattern not in code:
                errors.append(
                    f"self.name 与类名 {cls_name} 不一致"
                    f"(自定义 __init__ 时必须显式写 self.name = \"{cls_name}\")"
                )
        # 没有自定义 __init__:BaseSkill 自动处理,跳过检查

    # ── 保留工具名冲突检测 ────────────────────────────────────────────
    _reserved = _reserved_tool_names()
    for cls_name in class_names:
        if cls_name in _reserved:
            errors.append(f"类名 {cls_name} 与系统保留工具名冲突，禁止使用")
    _mname = _manifest_name_literal(tree)
    if _mname and _mname in _reserved and _mname not in class_names:
        errors.append(f"get_manifest() 的 name「{_mname}」与系统保留工具名冲突，禁止使用")

    # ── AST 副作用一致性校验 ──────────────────────────────────────────
    # 检测代码里的真实副作用 API,与 SkillSpec 声明对比
    # 声明 readonly 但代码含 file_write → 直接拒绝
    # ⭐ spec 缺失时改用**代码自己声明的**那一行（见 `declared_side_effects_from_code`）。
    # 这样审计窗口与部署时喂的是同一个输入，不会再出现
    # "窗口显示绿色可以部署、点下去说部署失败"（实测 图1）。
    _effective_side_effects = spec_side_effects
    if _effective_side_effects is None:
        _effective_side_effects = declared_side_effects_from_code(tree)
    if _effective_side_effects is not None:
        spec_side_effects = _effective_side_effects
        ast_warnings = detect_side_effects(code, tree)
        declared_none = (spec_side_effects == ["none"] or spec_side_effects == [])
        for w in ast_warnings:
            # 只有声明了 none/readonly 但实际有高危操作时才报错
            if declared_none or "none" in spec_side_effects:
                errors.append(
                    f"AST 副作用不一致:SkillSpec 声明 side_effects={spec_side_effects},"
                    f" 但代码含 {w}。请修改 SkillSpec 声明或移除该操作。"
                )
            else:
                # 其他情况作为警告记录到 log,不阻止注册
                logger.warning(f"[SkillWriter] AST 副作用提示: {w} (已在 spec 中声明,可接受)")

    return len(errors) == 0, errors


def check_manifest_shape(tree) -> str:
    """静态核一次 `get_manifest` 的**外层形状**，返回错误说明（没问题返回 ""）。

    判据与 `registry._manifest_name()` 完全一致：顶层要有 `name`，
    或者顶层是 `{"function_declarations": [{"name": ...}]}`。
    取不到 → registry 会把整条 manifest 丢掉，而**没有任何人被告知**
    （Skill 照样安装、UI 照样 READY、模型却看不见它）。

    ⚠️ 只在能静态看清时才报错。`get_manifest` 里如果是
    `return self._build()` 这类间接返回，我们看不到字典字面量 ——
    那种情况**放行**，宁可漏判也不要误拦一个写法更复杂但正确的 Skill。
    （漏判的代价回到现状：加载期一行 WARNING；误拦的代价是好 Skill 部署不了。）
    """
    import ast as _ast
    _fn = next(
        (n for n in _ast.walk(tree)
         if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
         and n.name == "get_manifest"),
        None,
    )
    if _fn is None:
        return ""
    _ret = next(
        (n for n in _ast.walk(_fn)
         if isinstance(n, _ast.Return) and isinstance(n.value, _ast.Dict)),
        None,
    )
    if _ret is None:
        return ""       # 看不清 → 放行（见 docstring）
    _keys = [k.value for k in _ret.value.keys
             if isinstance(k, _ast.Constant) and isinstance(k.value, str)]
    if "name" in _keys:
        return ""
    # 🔴 2026-08-29：`function_declarations`（Gemini 风格）**从放行改成拒绝**。
    #
    # 改造前这里放行它，而 `registry._manifest_name` 也接受它 —— 三方本来是
    # 一致的（提示词说别用，但用了也能工作）。这次删掉 registry 那个分支时，
    # 如果不同时收紧这里，就会变成：**审计说没问题、装上却不工作** ——
    # 比原来那个不一致更坏。
    # 📌 **要收紧一条规则，得把认它的地方一次收完** ——
    #    收一半，等于把「宽松但一致」换成「严格但自相矛盾」。
    # ⚠️ 而收紧本身是对的：这个提示词（见上方 get_manifest 的 WRONG 示例）
    #    早就写着 Gemini 风格是错的，项目也只接 Anthropic 了。
    if "function_declarations" in _keys:
        return (
            "get_manifest() 用了 Gemini 风格的列表外壳 "
            "{\"function_declarations\": [{...}]}，本项目只接受顶层形状 → "
            "**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
            "把 name / description / parameters 三个键放到最外层。"
        )
    if "function" in _keys and "type" in _keys:
        return (
            "get_manifest() 用了 OpenAI 风格的嵌套外壳 "
            "{\"type\":\"function\",\"function\":{...}}，registry 取不到顶层 name → "
            "**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
            "把 name / description / parameters 三个键放到最外层。"
        )
    return (
        f"get_manifest() 的返回里顶层没有 name 键（实际顶层键: {_keys or '空'}）→ "
        "registry 取不到名字，**整条 manifest 会被丢弃，Skill 装上了但模型看不见它**。"
        "name / description / parameters 必须在最外层。"
    )


def declared_side_effects_from_code(tree) -> list[str] | None:
    """从生成的代码里读出它**自己声明**的 side_effects。

    ═══ 为什么需要它（2026-08-05 实测）═══

    AST 副作用一致性校验的入口条件是 `if spec_side_effects is not None`，
    而两个调用点喂的东西不一样：

        审计窗口 `_emit_skill_preview` → 直接传 SkillSpec 阶段的值。
                                        spec 生成失败时是 **None** → **整个检查被跳过**
                                        → 窗口显示绿色"通过协议 v3.2 全部校验，可以部署"
        `_pending_skill` 落库时       → 存 `spec_side_effects or []` → 变成 **[]**
        部署 `apply_pending_skill`     → 传 `[]` → 检查运行 → declared_none → **拒绝**

    **同一段代码，两处结论相反**：用户看着绿色的"可以部署"点下去，被告知部署失败。
    这正是注释里描述过的那个体验问题的第二个实例（那次只把重名检测提前了）。

    ⚠️ 但**不能简单把 None 归一成 []**：那会误报。SkillSpec 阶段失败不等于
    "这个 Skill 声明了自己没有副作用" —— 生成的代码里那个 `get_spec()`
    可能声明得好好的（`side_effects=[SideEffect.SHELL]`）。拿它跟 `[]` 比就是
    比错了对象。

    **真正的权威是代码自己写的那一行** —— 部署之后生效的就是它，而不是那个
    中途失败的 SkillSpec。所以这里把它读出来，作为 spec 缺失时的取值。
    （同 `lifecycle` 的做法：那个字段本来就是从代码里 `re.search` 出来的。）

    返回 None 表示"代码里也没有可识别的声明"，此时保持旧行为（跳过检查）——
    那种情况下代码本身大概率连协议都不合，会被前面的结构检查先拦下。
    """
    import ast as _ast
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.keyword) or node.arg != "side_effects":
            continue
        val = node.value
        if not isinstance(val, (_ast.List, _ast.Tuple, _ast.Set)):
            continue
        out: list[str] = []
        for el in val.elts:
            # `SideEffect.SHELL` → "shell"
            if isinstance(el, _ast.Attribute):
                out.append(el.attr.lower())
            # 裸字符串 `"shell"`
            elif isinstance(el, _ast.Constant) and isinstance(el.value, str):
                out.append(el.value.lower())
        return out
    return None
