# core/os_layer/executor_vision.py
"""视觉定位器 VisionLocator —— 把「点击某个界面元素」的描述转成屏幕坐标。

两级降级（成本从低到高，能用便宜的就不用贵的）：
  1. UIA（uiautomation）—— 读控件树，匹配 Name/ControlType，拿真实坐标。又快又准。
  2. 多模态视觉 —— UIA 拿不到时（Canvas / 图片按钮 / 非标准控件），
     截图交给视觉模型。模型由「设置 → 进阶配置 → 视觉」指定，
     经 `core.models.model_for_role` 解析，**不在这里硬编码**。

⚠️ **OCR 那一级已于 2026-08-24 从链上拆掉**。
   `_locate_ocr` 保留在文件里但**零调用方**，理由见 `locate()` 的 docstring：
   它匹配的是「目标描述里出现过的词」而不是「目标」，
   且排序用的是 OCR 的识字置信度而非匹配度 —— 两个缺陷都不是调参能修的。
   ⚠️ 知识库那边的 OCR 是**另一套**（`core/rag.py`），不受此影响，仍在使用。

实现约束（三循环退出层级）：本类是「第 2 层 VisionLocator 内部降级」——
  只做「换定位手段」，不做「换计划」（replan 属于第 3 层）。
  走完降级链后只抛 SUCCESS / NOT_FOUND / AMBIGUOUS / OCCLUDED 四态之一，
  决策权上交。

返回形状：
    {"status": "SUCCESS",
     "candidates": [{"x":523,"y":341,"confidence":0.92,"label":"确定","source":"uia"}], ...}
"""
from __future__ import annotations
import asyncio
import os
import time
import re
import shutil
from typing import Any, Dict, List, Optional
from loguru import logger


# OCR 文字块匹配为"命中"的最低置信度（pytesseract conf 是 0-100）
_OCR_MIN_CONF = 40

# 两次枚举之间的间隔：用来筛掉【名字会变】的控件。
# ⚠️ 1.3 秒是按「系统时钟每秒刷新」定的下限 —— 实测这台机器上，
#    时钟是唯一一个两次枚举名字不同的控件（"系统时钟, 14:37:13" → "…:15"）。
# 📌 探针的名字必须稳定，否则**自己探到的探针自己定位不到**：
#    定位走的是双向包含匹配，"…14:37:13" 和 "…14:37:15" 两个方向都不包含。
_PROBE_SETTLE_SECONDS = 1.3

# 🔴🔴 **「可交互控件」的唯一判据** —— 定位器和探针选取器必须共用这一份。
#
# 2026-08-28 实测教训：第一版探针选取器**没有过滤 ControlType**，于是探到了
# 「运行中的应用程序」(PaneControl) 和「用户提示通知区域」(ToolBarControl)
# 这种**容器**。它们有 Name、在任务栏上真实存在，但定位器根本不认 ——
# 自检成功率当场变成 2/4 = 50%，**自己探到的探针自己找不到**。
# 📌 **探针的选取规则和定位器的匹配规则，必须来自同一处。**
#    两套规则各写各的，测出来的就不是"定位能力"，而是"两套规则一不一致"。
_CLICKABLE_TYPES = ("Button", "Text", "MenuItem", "ListItem", "Hyperlink",
                    "CheckBox", "RadioButton", "TabItem", "Edit", "ComboBox")


def _is_clickable_type(ctype: str) -> bool:
    return any(t in (ctype or "") for t in _CLICKABLE_TYPES)


def _has_visible_rect(ctrl) -> bool:
    """有正的宽高才算真的在屏幕上 —— 定位器最终要拿它算中心点。"""
    try:
        rect = ctrl.BoundingRectangle
        return bool(rect and rect.width() > 0 and rect.height() > 0)
    except Exception:
        return False


def _resolve_tesseract_cmd() -> Optional[str]:
    """解析 tesseract 可执行文件路径（与 rag.py 同一套逻辑，保持一致）。
    1. 环境变量 TESSERACT_CMD  2. PATH 里的 tesseract  3. Windows 默认安装路径
    都找不到返回 None，调用方应跳过 OCR 级。
    """
    env_path = os.getenv("TESSERACT_CMD")
    if env_path and os.path.exists(env_path):
        return env_path
    which_path = shutil.which("tesseract")
    if which_path:
        return which_path
    _default_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_default_win):
        return _default_win
    return None


class VisionLocator:
    def __init__(self, provider=None, screenshot_dir=None, model_override: Optional[str] = None):
        """
        Args:
            provider: ClaudeProvider 实例（多模态兜底用）
            screenshot_dir: 截图存放目录（pathlib.Path）
            model_override: 策略引擎里"OS视觉定位"任务类型解析出的模型id，
                None 时退化为 provider 的当前 target_model（即UI热切换菜单选的模型）。
        """
        import platform
        self._is_windows = platform.system() == "Windows"
        self._provider = provider
        self._screenshot_dir = screenshot_dir
        self._model_override = model_override
        self._tess_cmd_cache: Any = "__unresolved__"
        self._coord_check_done = False

    def _check_coord_consistency_once(self) -> None:
        """首次定位时校验一次坐标系一致性（DPI感知是否生效），只查一次。"""
        if self._coord_check_done:
            return
        self._coord_check_done = True
        try:
            from . import verify_coord_consistency
            ok, detail = verify_coord_consistency()
            if not ok:
                logger.warning(f"[Vision] 坐标系一致性校验未通过：{detail}（可能导致点击坐标系统性偏移）")
            else:
                logger.info(f"[Vision] 坐标系一致性校验通过：{detail}")
        except Exception as e:
            logger.warning(f"[Vision] 坐标系一致性校验异常: {e}")

    async def locate(self, target: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """定位语义目标。**两级降级：UIA → Claude 多模态。**

        ═══ 🔴🔴🔴 2026-08-24：OCR 那一级已从这条链上拆掉 ═══

        早先就定过这条（「把 UIA / OCR / 多模态里的 **ocr 全拆了**，
        只是让 ocr 不再用于 computer use，**知识库还是要用的**」），
        ⚠️ 上一批漏做了，然后实际运行中当场付了代价：

            指令   ：点一下屏幕上某个编辑器里叫"测试"的 session
            候选   ：{label: "Code", conf 0.94, source: "ocr"}
                     {label: "窗口", conf 0.86, source: "ocr"}

        那两个 label **不是屏幕上的东西，是【用户那句话里出现过的词】**。
        根因是 `_locate_ocr` 的匹配式：

            if target_lower in wl or (len(wl) >= 2 and wl in target_lower)
                                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            任何长度 ≥2 的 OCR 词，只要它是**目标描述**的子串，就算命中。

        📌 **它匹配的不是「目标」，是「目标描述里出现过的词」** ——
           于是**用户描述得越具体，误报越多**。这不是可以调参修好的东西，
           它的失败方向和「说清楚一点」是同向的。

        🔴 更糟的第二层：排序用的 `confidence` 是 **OCR 的识别置信度**
           （这个字读得清不清楚），**不是匹配度**。
           📌 **一个字读得越清楚，它越可能被当成答案。**
           而那道 `>= 0.15` 的闸在比较两个跟「像不像目标」毫无关系的数 ——
           那次差 0.08 才侥幸判了 AMBIGUOUS；差 0.16 就会直接点在 "Code" 那个字上。

        ⭐ `_locate_ocr` **保留但不再挂在链上**（知识库那边的 OCR 是另一套，
           一个字没动）—— 📌 删掉一个还有人可能想复用的实现，
           和把它从这条链上摘下来，是两件事；后者可逆。

        实现约束1：本方法是 VisionLocator 内部降级，只换定位手段，不做 replan。
        """
        self._check_coord_consistency_once()
        params = params or {}

        # ── 第一级：UIA ──────────────────────────────────────────────────
        uia_result = await asyncio.to_thread(self._locate_uia, target, False)
        if uia_result["status"] == "SUCCESS":
            return uia_result
        if uia_result["status"] == "AMBIGUOUS":
            # UIA 找到多个候选，直接返回让上层走 request_user_choice（不浪费下游算力）
            return uia_result

        # ── 第二级：Claude 多模态 ────────────────────────────────────────
        logger.info(f"[Vision] UIA 未命中「{target}」（{uia_result['status']}），"
                    f"转 Claude 多模态（OCR 已于 2026-08-24 从本链拆除）")
        return await self._locate_vision(target)

    async def locate_taskbar_canary(self, target: str) -> Dict[str, Any]:
        """canary 自检专用入口：只测 UIA 这一级对任务栏稳定按钮的定位是否健康，
        不降级到 OCR/Gemini（canary 要测的是"日常最依赖的那一级好不好用"，
        不是兜底链路；也避免每次自检都打 Gemini API 产生不必要的开销）。
        """
        return await asyncio.to_thread(self._locate_uia, target, True)

    async def discover_taskbar_probes(self, want: int = 4) -> List[str]:
        """现场探测任务栏上【当前真实存在、且名字稳定】的控件，当作自检探针。

        🔴🔴 **为什么不能写死一份目标名单**（2026-08-28 实测发现）：
        改造前 canary 拿一份写死的中文名单去找（`["任务视图", "显示桌面",
        "操作中心", "系统时钟"]`），于是把**两件完全不同的事混成了一个分数**：
        ```
        「这个控件不存在」  ← 用户的任务栏布局，**不是故障**
        「这个控件找不到」  ← UIA 定位能力坏了，**是故障**
        ```
        后果实测：
        ```
        某台中文 Win10        3/4 = 75%  ——「任务视图」根本没开，
                                            而 75% > 50% 阈值 ⇒ 不报警 ⇒ 问题被掩盖
        英文 Windows           0/4 =  0%  —— 名字全对不上 ⇒ 连续 3 次 ⇒ escalate
                                            ⇒ Nano 主动告诉用户「我的定位能力可能有问题」
                                            **一个假故障，而且会主动报警**
        ```
        ⚠️ 旧代码的注释里其实**知道**多语言会出问题，但"解法"是「应该走
           config 的 canary.targets」—— 而 config 里的值也是那四个中文。
           📌 **把问题挪进配置文件，不等于解决了它** ——
              默认值还是同一个错的值，而用户不会去改一个他不知道存在的东西。

        ⭐ 现在的做法：**一个名字都不写死**，运行时现取现用。
        ```
        1. 枚举 Shell_TrayWnd（这个 ClassName 是系统标准，语言无关）
        2. 等 _PROBE_SETTLE_SECONDS 再枚举一次
        3. 取交集 → 名字会变的（时钟）自动被筛掉
        4. ClassName 非空的（系统自带构件）优先，不够再补应用按钮
        ```
        📌 **探针的"会不会过期"这个问题，只存在于写死的方案里。现取现用就没有。**
        📌 用一次观察确定不了的事（名字稳不稳定），用**两次观察**去确定，
           而不是去维护一张"已知会变的名单"。

        ⚠️ 返回空列表**不等于健康度 0%** —— 它是「这次测不了」。
           这两件事必须由调用方分开处理，否则就退回到本次要修的那个 bug。
        """
        return await asyncio.to_thread(self._discover_taskbar_probes, want)

    def _discover_taskbar_probes(self, want: int) -> List[str]:
        if not self._is_windows:
            return []
        try:
            import uiautomation as auto
        except ImportError:
            return []

        def _snap():
            root = auto.PaneControl(ClassName="Shell_TrayWnd")
            if not root or not root.Exists(0, 0):
                return []
            out = []

            def walk(ctrl, depth=0):
                if depth > 12:
                    return
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    return
                for ch in children:
                    try:
                        n = (ch.Name or "").strip()
                        # ⚠️ 三个条件缺一不可，而且**必须和定位器用同一套**：
                        #    · 长度 >= 2   单字符名字在双向包含匹配里极易误命中
                        #    · 可交互类型  容器（Pane/ToolBar）有名字但定位器不认
                        #    · 有可见矩形  定位器最终要拿它算中心点
                        if (len(n) >= 2
                                and _is_clickable_type(ch.ControlTypeName or "")
                                and _has_visible_rect(ch)):
                            out.append((n, ch.ClassName or ""))
                    except Exception:
                        pass
                    walk(ch, depth + 1)

            walk(root)
            return out

        try:
            first = _snap()
            if not first:
                return []
            time.sleep(_PROBE_SETTLE_SECONDS)
            second = _snap()
        except Exception as e:
            logger.warning(f"[Canary] 探测任务栏探针失败: {e}")
            return []

        stable = {n for n, _ in first} & {n for n, _ in second}
        seen, cand = set(), []
        for n, cls in first:
            if n in stable and n not in seen:
                seen.add(n)
                cand.append((n, cls))
        # ClassName 非空 = 系统自带构件（TrayClockWClass 这类），比"某个正在运行
        # 的应用按钮"更不容易在两次调用之间消失。⚠️ 只作排序偏好，不作硬过滤 ——
        # Win11 的任务栏是 XAML 重写的，ClassName 可能全为空，硬过滤会直接归零。
        cand.sort(key=lambda x: not x[1])
        return [n for n, _ in cand[:max(1, want)]]

    # ── 第一级：UIA 控件树定位 ───────────────────────────────────────────

    def _locate_uia(self, target: str, use_taskbar: bool = False) -> Dict[str, Any]:
        if not self._is_windows:
            return self._mk("NOT_FOUND", target, error="UIA is only available on Windows")
        try:
            import uiautomation as auto
        except ImportError:
            return self._mk("NOT_FOUND", target, error="dependency_missing: uiautomation")

        try:
            if use_taskbar:
                # canary 自检专用：测试目标固定是任务栏稳定按钮（不依赖任何
                # 用户可能开着/没开着的临时窗口），直接定位任务栏窗口本身
                # （ClassName 'Shell_TrayWnd' 是 Windows 任务栏的标准窗口类名，
                # 系统启动后必然存在），不走 get_target_window() 的"排除自身/
                # 过滤小部件"逻辑——那套逻辑是为了"找用户真正想操作的窗口"，
                # 任务栏本身就是目标，不需要再去排除什么。
                root = auto.PaneControl(ClassName="Shell_TrayWnd")
                if not root or not root.Exists(0, 0):
                    return self._mk("OCCLUDED", target, error="taskbar window Shell_TrayWnd was not found")
            else:
                # 不直接信 OS 前台窗口——发消息这个动作本身会让 Nano 自己的窗口
                # 拿到焦点，GetForegroundControl() 拿到的几乎总是 Nano 自己，
                # 扫它的控件树永远找不到用户真正想点的东西。改用排除自身后的
                # 目标窗口（见 executor_low.get_target_window）。
                from .executor_low import get_target_window
                target_win = get_target_window()
                if target_win is None:
                    return self._mk("OCCLUDED", target, error="no usable non-Nano target window was found")
                root = auto.ControlFromHandle(target_win._hWnd)
                if not root:
                    return self._mk("OCCLUDED", target, error="failed to get the target window control tree")

            target_lower = target.lower()
            matches: List[Dict[str, Any]] = []

            # 深度遍历控件树，匹配可点击控件
            def walk(ctrl, depth=0):
                if depth > 12:
                    return
                try:
                    name = (ctrl.Name or "").strip()
                    ctype = ctrl.ControlTypeName or ""
                    # 匹配条件：名字与目标互相包含（双向），且是可交互控件。
                    # 双向而非单向：模型描述目标时常带后缀（"文件菜单"/"文件按钮"），
                    # 若只判 target ⊆ name，控件真实 Name 只是"文件"时永远判不中。
                    # name⊆target 方向要求 name 长度>=2：避免"5"这种单字符偶然成为
                    # target（如"Microsoft 365"）子串而误判命中。
                    #
                    # 复现出来的二次缺陷（2026-06-20）：name⊆target 方向原来是
                    # "name 在 target 任意位置出现就算命中"，当 target 是一整句话
                    # （比如"记事本菜单栏的查看按钮"）时，"记事本""菜单"这类无关
                    # 短词只要恰好是某个控件的真实 Name，就会被随意命中，制造出
                    # 一堆假阳性 AMBIGUOUS（用户原话"整个屏幕都只有一半Nano一半
                    # 记事本，哪来的好几个像"——根本不是真的有歧义，是匹配过松）。
                    # 收紧为：必须紧跟在 name 后面的要么是字符串结尾，要么是泛化
                    # UI 后缀词（按钮/菜单/...），还原"name + 后缀"这种受控的命中
                    # 模式，不再允许 name 出现在句子中间任意位置就算数。
                    nl = name.lower()
                    _hit = False
                    # AutomationId 精确相等也算命中：它不随界面语言变（中文计算器的「加」
                    # 按钮 id 是 plusButton），模型可以用 read_window_tree 里看到的 id 当 target。
                    _aid = (getattr(ctrl, "AutomationId", "") or "").strip().lower()
                    _aid_hit = bool(_aid) and _aid == target_lower
                    if _aid_hit:
                        _hit = True
                    elif name:
                        if target_lower in nl:
                            _hit = True
                        elif len(nl) >= 2 and nl in target_lower:
                            _idx = target_lower.find(nl)
                            _after = target_lower[_idx + len(nl):]
                            _hit = (_after == "" or any(
                                _after.startswith(s) for s in
                                ("按钮", "菜单", "选项", "图标", "链接", "标签", "菜单项",
                                 "项", "栏", "button", "menu", "icon", "tab", "link")
                            ))
                    if _hit:
                        if _is_clickable_type(ctype):
                            try:
                                rect = ctrl.BoundingRectangle
                                if rect and rect.width() > 0 and rect.height() > 0:
                                    cx = (rect.left + rect.right) // 2
                                    cy = (rect.top + rect.bottom) // 2
                                    matches.append({
                                        "x": cx, "y": cy,
                                        "confidence": (0.97 if _aid_hit else
                                                       0.95 if name.lower() == target_lower else 0.85),
                                        "label": name or _aid, "source": "uia",
                                        "control_type": ctype,
                                    })
                            except Exception:
                                pass
                    for child in ctrl.GetChildren():
                        walk(child, depth + 1)
                except Exception:
                    pass

            walk(root)

            # 按坐标去重：UIA 经常把同一个视觉元素的不同控件模式
            # （MenuItemControl/ButtonControl/TextControl 等）都暴露成独立节点，
            # 复现"点击查看按钮"返回3个候选，但坐标完全相同(141,52)——
            # 不是真的有歧义，是同一个东西被数了三遍。坐标四舍五入到5px容差
            # 内视为同一点，只保留其中置信度最高的那条。
            _dedup: Dict[tuple, Dict[str, Any]] = {}
            for m in matches:
                key = (round(m["x"] / 5), round(m["y"] / 5))
                if key not in _dedup or m["confidence"] > _dedup[key]["confidence"]:
                    _dedup[key] = m
            matches = list(_dedup.values())

            if not matches:
                return self._mk("NOT_FOUND", target)
            if len(matches) == 1:
                return self._mk("SUCCESS", target, candidates=matches)
            # 多个匹配：按 confidence 排序，如果最高的明显胜出就用它，否则 AMBIGUOUS
            matches.sort(key=lambda m: m["confidence"], reverse=True)
            if matches[0]["confidence"] - matches[1]["confidence"] >= 0.1:
                return self._mk("SUCCESS", target, candidates=[matches[0]])
            logger.info(
                f"[Vision] UIA 命中多个候选「{target}」: "
                + str([(m["label"], m["control_type"], m["x"], m["y"], m["confidence"]) for m in matches[:8]])
            )
            return self._mk("AMBIGUOUS", target, candidates=matches[:5])

        except Exception as e:
            logger.error(f"[Vision] UIA 定位异常: {e}")
            return self._mk("NOT_FOUND", target, error=str(e))

    # ── 第二级：OCR（pytesseract）定位 ───────────────────────────────────

    def _resolve_tess(self) -> Optional[str]:
        if self._tess_cmd_cache == "__unresolved__":
            self._tess_cmd_cache = _resolve_tesseract_cmd()
        return self._tess_cmd_cache

    def _locate_ocr(self, target: str) -> Dict[str, Any]:
        """截图 → pytesseract.image_to_data 拿文字块+坐标 → 匹配目标关键词。

        与 RAG 层用同一个 OCR 引擎（pytesseract），但用 image_to_data（带坐标）
        而非 image_to_string（纯文字），因为视觉定位需要中心点坐标。
        """
        tess_cmd = self._resolve_tess()
        if tess_cmd is None:
            return self._mk("NOT_FOUND", target, error="dependency_missing: tesseract was not found; set TESSERACT_CMD or install Tesseract-OCR")

        try:
            import pytesseract
            from pytesseract import Output
            from PIL import Image
        except ImportError as e:
            return self._mk("NOT_FOUND", target, error=f"dependency_missing: {e} (pip install pytesseract pillow)")

        # 截图（OCR 需要全屏图，顺带留作 screenshot_ref 供标注）
        shot_path, shot_size = self._take_screenshot()
        if shot_path is None:
            return self._mk("OCCLUDED", target, error="OCR screenshot failed")

        try:
            pytesseract.pytesseract.tesseract_cmd = tess_cmd
            img = Image.open(str(shot_path))
            data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT)

            target_lower = target.lower()
            n = len(data.get("text", []))
            matches: List[Dict[str, Any]] = []
            for i in range(n):
                word = (data["text"][i] or "").strip()
                if not word:
                    continue
                try:
                    conf = float(data["conf"][i])
                except (ValueError, TypeError):
                    conf = -1.0
                if conf < _OCR_MIN_CONF:
                    continue
                # word⊆target 方向要求 word 长度>=2，避免单字符偶然命中（同UIA同款规避）
                wl = word.lower()
                if target_lower in wl or (len(wl) >= 2 and wl in target_lower):
                    left, top = int(data["left"][i]), int(data["top"][i])
                    w, h = int(data["width"][i]), int(data["height"][i])
                    cx, cy = left + w // 2, top + h // 2
                    matches.append({
                        "x": cx, "y": cy,
                        "confidence": round(conf / 100.0, 2),  # 归一到 0-1，与 UIA 对齐
                        "label": word, "source": "ocr",
                    })

            if not matches:
                return self._mk("NOT_FOUND", target, screenshot_ref=str(shot_path))
            if len(matches) == 1:
                return self._mk("SUCCESS", target, candidates=matches, screenshot_ref=str(shot_path))
            matches.sort(key=lambda m: m["confidence"], reverse=True)
            if matches[0]["confidence"] - matches[1]["confidence"] >= 0.15:
                return self._mk("SUCCESS", target, candidates=[matches[0]], screenshot_ref=str(shot_path))
            return self._mk("AMBIGUOUS", target, candidates=matches[:5], screenshot_ref=str(shot_path))

        except Exception as e:
            logger.error(f"[Vision] OCR 定位异常: {e}")
            return self._mk("NOT_FOUND", target, error=str(e))

    # ── 第三级：多模态视觉定位（Claude）────────────────────────────────────

    async def _locate_vision(self, target: str) -> Dict[str, Any]:
        """Claude 多模态兜底定位。

        验收实测发现：宽屏截图（如1920x1080，16:9）喂给视觉模型时，
        返回的 X 坐标会出现系统性失真（换成正方形裁图后误差从374px降到66px）。
        Claude 的坐标定位精度需重新校验，Gemini 时代的偏移修正值不一定适用。

        缓解方案：曾试过"补黑边垫成正方形"（padding），实测在真实复杂画面下
        效果更差——模型会把坐标算到黑边的假想空间里（验证时拿到过 y=-115 这种越界值），
        说明黑边没有被模型当成"无内容"区域处理，反而扰乱了坐标换算。
        改用真裁剪（不留黑边，直接裁掉两侧多出来的部分，居中）：所有像素都是真实画面，
        这是目前验证下来效果最好的方案。代价：屏幕最左/最右两侧各约
        (宽-边长)/2 的区域会被裁掉，目标如果恰好在这个盲区会被误判 NOT_FOUND
        （安全的失败模式——不会点错，只是这次找不到，三级降级走完会如实上报）。

        二次实测发现两类更严重的问题（不是坐标精度，是"认错对象"）：
        ① 画面有相似/同名元素时（如"Edge登录"vs另一窗口"QQ登录"、"Microsoft 365"
           vs"Microsoft Store"），模型会直接选错，且自报confidence和选对时一样高
           （0.95），不能用confidence判断对错。
        ② 要单点坐标会把"图标中心/按钮中心/语义目标中心"混在一起，越界/语义都
           没法单独校验。Gemini官方推荐的空间定位输出本就是bbox（box_2d），不是
           单点——改成要bbox，本地取中心点，至少能做"点是否在前台窗口范围内"
           这类合法性校验。

        应对：① prompt明确要求模型发现多个相似候选时全部列出来，不强行选一,
        返回多个候选时直接判 AMBIGUOUS（复用UIA/OCR两级已有的歧义上报机制，
        不再让Gemini这级绕过歧义控制变成"永远自信给一个坐标"的逃逸通道）。
        ② 即使只有一个候选，也校验候选中心点是否落在当前前台窗口范围内，
        不在→直接判 NOT_FOUND（这条直接命中"跨窗口认错"的真实复现案例）。
        """
        if self._provider is None:
            return self._mk("NOT_FOUND", target, error="no provider is available; visual fallback is unavailable")

        # 截图
        shot_path, shot_size = await asyncio.to_thread(self._take_screenshot)
        if shot_path is None:
            return self._mk("OCCLUDED", target, error="screenshot failed")

        # ⭐⭐⭐ 2026-08-24：本级重做成**两遍，且由模型自己驱动**。
        #
        # ═══ 为什么必须有第二遍 ═══
        # 🔴 实际运行：让 Nano 点某个编辑器里那个叫「测试」的 session，
        #    整屏喂进去它**看不见那两个字**（「不放大确实看不到，
        #    这个之前造这部分的时候就测试过了，目前是一样的结果」）。
        # ⭐ 而决定性的论据是：
        #    > **它就算看不到那个"测试"俩字，它总能看到 Claude Code 窗口在屏幕什么位置吧**
        #    ⇒ 它**永远有能力指出该放大哪一块** —— 所以这条链没有失败模式。
        # ⭐ 而且这套**项目里已经在跑**（`look_at_screen` 的两遍放大），
        #    📌 一个已经存在的形状，第二次出现时该复用它。
        #
        # ═══ 🔴 为什么本级不再报 AMBIGUOUS（推翻了早先的结论）═══
        # 旧注释的理由是「不让这一级变成『永远自信给一个坐标』的逃逸通道」。
        # 那条防的是**编造的自信**，而这里说的是另一件事：
        #
        #   UIA 的歧义   = 【信息不足】两个控件名字一模一样，它手上没有能分辨的东西
        #                  ⇒ 问用户是**唯一出路**  ✅ 那一级保留 AMBIGUOUS
        #   多模态的歧义 = 【它看得见画面】它知道用户在干什么、两个候选长什么样
        #                  ⇒ 问用户是把自己该做的判断推出去
        #
        # 📌 **该不该问用户，取决于「这一级手上有没有能分辨的信息」，
        #    不取决于「有几个候选」。**
        # 🔴 旧写法把「我不知道」和「我懒得判断」写成了同一个状态。
        # ⚠️ 保护没有因此消失：风险确认弹窗（带标注图，用户看得见要点哪）
        #    与「落点必须在目标窗口内」那道校验**一条都没动**。
        #
        # ⚠️ 刻意**没有**给本方法加一个「用户原话」参数：`target` 本来就是
        #    Nano 写的、已经带着意图（实际那句就是「…中**第一个**叫"测试"的…」）。
        #    📌 一个字段能表达的东西不该开第二个入口 —— 两个来源迟早会打架。
        try:
            w, h = shot_size
            # ── 第一遍：整屏（等比缩放，不裁、不补边）──────────────────────
            # ⭐⭐⭐ [2026-08-24] **坐标走【图上像素】，图按官方公式压到上限内。**
            #
            # 🔴 这里此前走过两条错路，两条都是自己发明的格式：
            #   ① 居中裁正方形（`_crop_to_square`）—— 给 Gemini 打的补丁，
            #      代价是屏幕左右各 420px 永远进不了视野。已删。
            #   ② 0–1 归一化坐标 —— 当时以为「数学上更干净、不怕对方偷缩图」，
            #      **但那不是模型被训练的格式**。官方 computer use 的契约原文：
            #        > Every coordinate is in the pixel space of the screenshots you return.
            #        > Claude always returns coordinates in the space of the images it sees.
            #      实测代价：x 的误差从 ≈0 变成了 +37 / +102。
            #      📌 **模型不是按数学工作的，是按它见过的分布工作的。**
            #
            # ⭐ 现在两件事一起做，缺一不可：
            #   · `_fit_scale` 同时满足**长边 1568** 和**总像素 1.15M** ——
            #     此前只按长边算，发出去 1568×882 = 1.38MP，超限 20%，
            #     **对方自己缩到 1430×804 而我们不知道**。
            #     📌 只要还有一次我们不知道的缩放夹在中间，后面所有换算都是在猜。
            #   · 坐标问「这张图上的像素」，我们只做一次除法。
            #     ⇒ 「我们发的」== 「它看到的」，换算才成立。
            scaled_bytes, scale, sw, sh = await asyncio.to_thread(
                self._scale_for_vision, shot_path, w, h)
            data = await self._ask_vision(scaled_bytes, target, sw, sh, allow_zoom=True)
            if not data:
                return self._mk("NOT_FOUND", target, screenshot_ref=str(shot_path))

            # 图上像素 → 屏幕：一次除法。
            parsed = self._to_screen(data.get("candidates"), w, h,
                                     lambda px, py: (int(px / scale), int(py / scale)))
            self._diag("第一遍(整屏)", target, data.get("candidates"), parsed,
                       发出的图=f"{sw}x{sh}", 屏=f"{w}x{h}", scale=round(scale, 4),
                       need_zoom=bool(data.get("need_zoom")),
                       zoom_box=data.get("zoom_box"))

            # ── 第二遍：模型自己说「放大这块」───────────────────────────────
            # ⚠️ 触发条件是 **它自己要求** 或 **第一遍什么都没找到** ——
            #    📌 「要不要放大」只有它知道；系统替它决定就回到了写死策略。
            zbox = data.get("zoom_box") if (data.get("need_zoom") or not parsed) else None
            if zbox and len(zbox) == 4:
                try:
                    # zoom_box 也是**第一遍那张图上的像素** → 同一次除法还原到屏幕。
                    # ⚠️ 不假设模型给的是「左,上,右,下」有序 —— 排一下序。
                    #    📌 按有序处理的错法是**裁出一个负宽的框**。
                    zx0, zx1 = sorted((float(zbox[0]) / scale, float(zbox[2]) / scale))
                    zy0, zy1 = sorted((float(zbox[1]) / scale, float(zbox[3]) / scale))
                    zx0, zy0 = max(0, int(zx0)), max(0, int(zy0))
                    zx1, zy1 = min(w, int(zx1)), min(h, int(zy1))
                    if zx1 - zx0 >= 40 and zy1 - zy0 >= 40:
                        crop_bytes, cscale, cw, ch = await asyncio.to_thread(
                            self._crop_and_scale, shot_path, (zx0, zy0, zx1, zy1))
                        # ⚠️ 第二遍**不再允许它继续要放大** ——
                        #    📌 一个可以无限自我延长的循环，缺的不是出口是上界。
                        d2 = await self._ask_vision(crop_bytes, target, cw, ch,
                                                    allow_zoom=False)
                        if d2 and d2.get("candidates"):
                            # 放大图上像素 → 裁剪区内 → 屏幕：先除 cscale，再加左上角。
                            p2 = self._to_screen(
                                d2.get("candidates"), w, h,
                                lambda px, py: (int(zx0 + px / cscale),
                                                int(zy0 + py / cscale)))
                            self._diag("第二遍(放大)", target,
                                       d2.get("candidates"), p2,
                                       裁剪=f"{zx0},{zy0}-{zx1},{zy1}",
                                       发出的图=f"{cw}x{ch}", cscale=round(cscale, 4))
                            if p2:
                                parsed = p2
                                logger.info(f"[Vision] 放大第二遍命中「{target}」"
                                            f"（区域 {zx0},{zy0}-{zx1},{zy1}）")
                except Exception as _ez:
                    logger.warning(f"[Vision] 放大第二遍失败（用第一遍的结果）: {_ez}")

            if not parsed:
                return self._mk("NOT_FOUND", target, screenshot_ref=str(shot_path))

            # ⭐ 多个候选 → **它自己挑**（按它给的 confidence），不再上报歧义。
            parsed.sort(key=lambda c: c.get("confidence", 0.0), reverse=True)
            cand = parsed[0]
            if len(parsed) > 1:
                logger.info(f"[Vision] 「{target}」有 {len(parsed)} 个候选，"
                            f"按模型给的置信度取「{cand.get('label')}」"
                            f"（这一级自己判断，不问用户）")

            # ⚠️ 落点必须在目标窗口内 —— 这条**没动**。
            #    命中过「跨窗口认错」的真实案例（要点窗口 A 的登录按钮，点到了 B 的）。
            fg_rect = await asyncio.to_thread(self._foreground_window_rect)
            if fg_rect and not self._point_in_rect(cand["x"], cand["y"], fg_rect):
                return self._mk("NOT_FOUND", target, screenshot_ref=str(shot_path),
                                error=f"candidate point ({cand['x']},{cand['y']}) is outside the foreground target window; likely wrong window")

            return self._mk("SUCCESS", target, candidates=[cand],
                            screenshot_ref=str(shot_path))

        except Exception as e:
            logger.error(f"[Vision] Gemini 定位异常: {e}")
            return self._mk("NOT_FOUND", target, error=str(e))

    async def _ask_vision(self, png_bytes: bytes, target: str,
                          iw: int, ih: int, allow_zoom: bool):
        """问一次视觉模型：在这张 `iw×ih` 的图里找 `target`。

        返回 `{"candidates":[{box,label,confidence}], "need_zoom", "zoom_box"}` 或 None。

        ⭐⭐⭐ [2026-08-24] **坐标要【这张图上的像素】，不是归一化。**

        🔴 上一版改成了 0–1 归一化浮点，理由是「数学上更干净、不怕对方偷偷缩图」。
           **那是自己发明的格式。** 官方 computer use 的契约原文是：
             > Every coordinate is in **the pixel space of the screenshots you return**.
             > Claude always returns coordinates **in the space of the images it sees**.
           ⇒ 模型是被训练成「在你发的那张图上报像素」的。
        📌 **把一个模型被训练过的输出格式，换成了一个数学上更漂亮的格式** ——
           数学上两者等价，**但模型不是按数学工作的，是按它见过的分布工作的。**
        ⚠️ 而实测正好对上：改成归一化之后 **x 的误差从 ≈0 变成了 +37 / +102**。

        ⚠️ 现在可以放心把尺寸写进提示词了：`_fit_scale` 保证这张图**不会再被对方改**，
           所以「我们说的尺寸」就是「它看到的尺寸」。
           📌 上一版之所以不敢写，正是因为那时候这句话不成立。

        ⚠️ `allow_zoom=False` 时**连提都不提放大** —— 📌 一个在提示词里被提到的选项，
           模型迟早会选它；要禁掉一个分支，最干净的是让它在那一遍里根本不存在。
        """
        zoom_clause = (
            'If the target is too small or blurry to locate confidently, do NOT guess. '
            'Instead set "need_zoom": true and give "zoom_box" for a REGION worth '
            'magnifying - the panel, window, or list that most likely contains it. '
            'You can almost always name that region even when you cannot read the '
            'target itself (for example: the app window it belongs to). '
            'Make the region generous rather than tight.\n'
            if allow_zoom else
            'This is already a magnified view. Answer from what you can see.\n'
        )
        prompt = (
            f"This is a {iw}x{ih} pixel screenshot. "
            f"Find the UI element described as: {target!r}.\n"
            f"{zoom_clause}"
            "If several elements plausibly match, list them all and rank them by how well "
            "they fit the description - including any ordering words in it such as "
            "'the first one'. You are expected to make that judgement yourself.\n"
            "Output only one JSON object and no other text:\n"
            '{"found": true/false, "candidates": ['
            '{"box": [left, top, right, bottom], "label": "visible text or visual label", '
            '"confidence": 0.0 to 1.0}, ...]'
            + (', "need_zoom": true/false, "zoom_box": [left, top, right, bottom] or null'
               if allow_zoom else '')
            + "}\n"
            f"Every box and zoom_box is in PIXELS of THIS {iw}x{ih} image, with the "
            f"top-left corner at (0,0) and the bottom-right at ({iw},{ih}). "
            "Give the tight bounding box of the element.\n"
            'If nothing is found, output {"found": false, "candidates": []}.'
        )
        try:
            img_part = self._provider.build_image_part(png_bytes, "image/png")
            context = [{"role": "user",
                        "content": [{"type": "text", "text": prompt}, img_part]}]
            content, _ = await self._provider.chat_without_tools(
                context,
                "You are a precise visual UI locator. Output JSON only, with all "
                "coordinates in pixels of the image you were given. When several "
                "elements match, rank them yourself instead of refusing to choose.",
                model_override=self._model_override,
            )
            return self._parse_json(content)
        except Exception as e:
            logger.warning(f"[Vision] 视觉调用失败: {e}")
            return None

    @staticmethod
    def _to_screen(raw_candidates, w: int, h: int, to_xy):
        """模型给的 box 列表（**图上像素**）→ 屏幕坐标候选列表。越界的丢掉。

        ⚠️ `to_xy` 由调用方给，因为两遍的换算不一样：
           第一遍只有一次除法，第二遍是「除完再加裁剪区左上角」。

        ⚠️ `to_xy` 由调用方给，因为**两遍的换算不一样**：
           第一遍只有缩放，第二遍是偏移 + 缩放。
           📌 把换算作为参数传进来，比在这里写 `if 是第几遍` 干净 ——
              后者会让这个函数知道它不该知道的事。
        """
        out = []
        for c in (raw_candidates or []):
            box = c.get("box")
            if not box or len(box) != 4:
                continue
            try:
                x1, y1, x2, y2 = [float(v) for v in box]
            except (TypeError, ValueError):
                continue
            cx, cy = to_xy((x1 + x2) / 2, (y1 + y2) / 2)
            if cx < 0 or cy < 0 or cx > w or cy > h:
                continue
            out.append({"x": cx, "y": cy,
                        "confidence": float(c.get("confidence", 0.7)),
                        "label": c.get("label", ""), "source": "claude_vision"})
        return out

    @staticmethod
    def _crop_and_scale(shot_path, box):
        """从**全分辨率原图**裁 `box` 那块，放大到官方上限内。
        返回 `(png, cscale, w, h)`，`cscale = 输出 / 裁剪区`。

        ⭐ 裁的是**原图**不是第一遍那张缩过的图 —— 📌 从已经丢过信息的图上
           再放大，放大的是马赛克。
        ⚠️ 这里**允许放大**（第二遍要的就是放大），但和第一遍走**同一套上限**：
           长边 1568 且总像素 ≤1.15M。
           🔴 上一版按「宽 1568」放大，把 208×490 拉成 1568×3694 = 5.8 兆像素，
              超限 5 倍 —— 对方缩回去，我们的换算全错。
        """
        from PIL import Image
        import io
        img = Image.open(str(shot_path)).convert("RGB").crop(tuple(int(v) for v in box))
        _w, _h = img.width, img.height
        cscale = VisionLocator._fit_scale(_w, _h, allow_upscale=True)
        out_w, out_h = max(1, int(_w * cscale)), max(1, int(_h * cscale))
        if (out_w, out_h) != (_w, _h):
            img = img.resize((out_w, out_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), (out_w / max(1, _w)), out_w, out_h

    @staticmethod
    def _diag(tag: str, target: str, raw, mapped, **ctx) -> None:
        """把**模型给的原始 box** 和**我们换算出来的屏幕坐标**并排打出来。

        ═══ 为什么必须有这一行（2026-08-24 实测）═══
        🔴 三次点击**稳定偏了整整一行**（-24~-35px，行高 28px），
           而标注图上的标签写的是「将点击: 测试」——
           **模型认对了是什么，框错了在哪。**
        ⚠️ 而当时两个假设产生的现象**完全一样**，手上的数据一个都分不出来：
             (a) 模型给的 box 本来就偏了一行
             (b) 我们的换算把它挪了一行
           换算的单元测试是精确往返的，但**模型的原始 box 没有任何地方记下来**。
        📌 **一个只记录结果、不记录输入的流程，出了偏差就只能靠猜谁的错** ——
           而这一带已经连着猜错过三次（`_crop_to_square` 那条补偿就是这么来的）。
        ⭐ 所以这不是「顺手加个日志」，是**把一次推理换成一次测量**：
           下一次实测跑完，这个问题自己就答了。
        ⚠️ 只打前 3 个候选：📌 一条要被人读的日志，长到要横向滚就没人读了。
        """
        try:
            _r = [(c.get("label"), c.get("box"), c.get("confidence"))
                  for c in (raw or [])[:3]]
            _m = [(c.get("label"), c.get("x"), c.get("y")) for c in (mapped or [])[:3]]
            logger.info(f"[Vision-Diag] {tag}「{target}」 {ctx} | "
                        f"模型原始 box={_r} | 换算后屏幕坐标={_m}")
        except Exception:
            pass

    @staticmethod
    def _foreground_window_rect():
        """目标窗口（排除 Nano 自己）的边界矩形 (left, top, right, bottom)，
        拿不到返回 None。

        这条校验本来是用来防"跨窗口认错"的（候选点必须落在目标窗口范围内），
        但之前用的是裸前台窗口——Nano 自己几乎总是前台，导致它非但没拦住
        "认错窗口"，反而把"落在 Nano 自己窗口里的错误坐标"当成合法通过。
        """
        try:
            from .executor_low import get_target_window
            w = get_target_window()
            if not w:
                return None
            return (w.left, w.top, w.right, w.bottom)
        except Exception:
            return None

    @staticmethod
    def _point_in_rect(x: int, y: int, rect) -> bool:
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    # ⭐⭐⭐ [2026-08-24] **官方公式：长边 与 总像素，两个都要满足。**
    #
    # 🔴 之前只按长边算（1568/1920 = 0.8167）→ 发出去 1568×882 = **1.38 兆像素**，
    #    而上限是 **1.15 兆像素** —— 超了 20%，于是**对方自己把它缩到 1430×804**。
    #    📌 我们从头到尾都在发一张**会被别人改的图**，而改完多大我们不知道。
    # ⭐ 官方文档给的就是下面这三行（`min` 里那第三项正是当初漏掉的）。
    _VISION_MAX_EDGE = 1568        # 长边上限
    _VISION_MAX_PX = 1_150_000     # 总像素上限

    @staticmethod
    def _fit_scale(w: int, h: int, allow_upscale: bool = False) -> float:
        """算出「发出去不会再被对方改」的缩放比例。**照抄官方公式。**

        ⚠️ `min` 里必须有**三项**：`1.0`（不放大）、长边、总像素。
           📌 上一版漏了总像素那一项 —— 而漏掉的表现不是报错，
              是**对方替我们缩，然后我们按自己那份尺寸去换算**。
        ⚠️ `allow_upscale=True` 只给放大那一遍用（第二遍要的就是放大），
           但**仍然贴着同样的上限**：📌 要既拿到放大、又不被别人改，
           就得自己把图放到上限的内侧。
        """
        if w <= 0 or h <= 0:
            return 1.0
        _cap = min(VisionLocator._VISION_MAX_EDGE / max(w, h),
                   (VisionLocator._VISION_MAX_PX / (w * h)) ** 0.5)
        return _cap if allow_upscale else min(1.0, _cap)

    @staticmethod
    def _scale_for_vision(shot_path, w: int, h: int):
        """整屏截图缩到**官方上限以内**。返回 `(png_bytes, scale, out_w, out_h)`。

        `scale = 输出 / 原图`，所以模型给的像素坐标 **÷ scale** 就是屏幕坐标。

        ⚠️ 之所以自己缩，不是为了迁就哪个模型 —— 是为了**让「我们发的」和
           「它看到的」是同一张图**。📌 只要还有一次我们不知道的缩放夹在中间，
           后面所有的换算都是在猜。
        """
        from PIL import Image
        import io
        scale = VisionLocator._fit_scale(w, h)
        img = Image.open(str(shot_path)).convert("RGB")
        out_w, out_h = max(1, int(w * scale)), max(1, int(h * scale))
        if (out_w, out_h) != (w, h):
            img = img.resize((out_w, out_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), (out_w / w), out_w, out_h


    # ── 工具方法 ─────────────────────────────────────────────────────────

    def _take_screenshot(self):
        """截图，返回 (path, (w,h))。失败返回 (None, None)。"""
        try:
            import mss, mss.tools
            import datetime as _dt
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                if self._screenshot_dir is None:
                    return None, None
                fname = f"locate_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                path = self._screenshot_dir / fname
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))
                return path, (shot.width, shot.height)
        except Exception as e:
            logger.error(f"[Vision] 截图失败: {e}")
            return None, None

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        import json
        # 剥 markdown 代码块
        text = re.sub(r"```(?:json)?", "", text).strip()
        try:
            return json.loads(text)
        except Exception:
            # 尝试抓第一个 {...}
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return None
            return None

    @staticmethod
    def _mk(status: str, target: str, candidates: Optional[List[Dict]] = None,
            screenshot_ref: str = "", error: str = "") -> Dict[str, Any]:
        """⭐ [2026-08-24] 多带一个 `summary`：**哪一级给的 + 候选各自叫什么**。

        🔴 实际运行中：定位返回 AMBIGUOUS + 2 个候选（label 分别是 `Code` 和 `窗口`），
           而 Nano 说的是「**找到了。有两个"测试"的 session。我点第一个。**」
           —— 它没看 label，把「AMBIGUOUS + 2 个候选」读成了「找到了 2 个你要的」。
        📌 **`AMBIGUOUS` 这个词本身在误导**：它的字面意思是
           「找到多个，不知道选哪个」，而实际可能是
           「一个都没找到，只是有两个字碰巧对上了」——
           **「歧义」和「误报」被塞进了同一个状态**，而模型只看得到状态名。
        ⭐ 修法不是新增状态（那要改整条状态机），是**把候选的名字摆到它眼前**：
           📌 让模型自己看见 `Code` / `窗口` 跟它要找的东西根本不是一回事，
              比我们替它下判断可靠。
        ⚠️ 仍然只陈述事实，不替它决定 —— 判断留给模型（同「系统答发生了什么，
           模型答所以怎么办」）。
        """
        cands = candidates or []
        _sum = ""
        if cands:
            _src = ", ".join(sorted({str(c.get("source") or "?") for c in cands}))
            _labels = "; ".join(f"{c.get('label') or '?'!s}"
                                f"(x={c.get('x')},y={c.get('y')})" for c in cands[:5])
            _sum = (f"located by {_src}; {len(cands)} candidate(s) matched, "
                    f"labelled: {_labels}. "
                    f"The labels are the text actually on screen - check whether they "
                    f"really are what you asked for before acting on them.")
        return {
            "status": status,
            "target": target,
            "candidates": cands,
            "screenshot_ref": screenshot_ref,
            "error": error,
            "summary": _sum,
        }
