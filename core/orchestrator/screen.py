# core/orchestrator/screen.py
"""看屏幕与视觉：截图、视觉模型问答、图片记录与回看、窗口模式。（`Orchestrator` 的 mixin）"""

import asyncio
import re
import time
from typing import Optional

from loguru import logger

from core.orchestrator._types import ToolOutcome
from core.tools.manifests import _NL


def _vision_model_for_os() -> Optional[str]:
    """OS 层视觉定位（computer use 看屏幕）该用哪个模型。

    📌 与 `core/rag.py:_vision_model()` 同一条判据：视觉是独立角色槽，
       `vision_for()` 空串 = 退回主模型（返回 None 让下游沿用默认）。
    ⚠️ 这个参数一直存在，但两个调用点长期传 None ⇒ 视觉恒用主模型。
       在只有 Claude 一家、且全系都有视觉时看不出问题 ——
       换成"某些型号有视觉、某些没有"的厂商时才会卡死。
    """
    try:
        from core.models import vision_for
        from core.provider import provider as _p
        main = getattr(_p, "target_model", "") or ""
        v = vision_for(main)
        return v or None          # None = 沿用主模型
    except Exception:
        return None


class ScreenMixin:
    """看屏幕与视觉：截图、视觉模型问答、图片记录与回看、窗口模式。"""

    # GUI 任务的空闲兜底时长（没有进行中的轮、没有待唤醒的挂起时，空闲这么久就结束任务）。
    _GUI_TASK_IDLE_SEC = 15 * 60

    _LOOK_FRACS = {
        "left": (0, 0, 0.5, 1), "right": (0.5, 0, 1, 1),
        "top": (0, 0, 1, 0.5), "bottom": (0, 0.5, 1, 1),
        "center": (0.25, 0.25, 0.75, 0.75),
        "top_left": (0, 0, 0.5, 0.5), "top_right": (0.5, 0, 1, 0.5),
        "bottom_left": (0, 0.5, 0.5, 1), "bottom_right": (0.5, 0.5, 1, 1),
    }

    def _capture_screen_image(self):
        """抓主屏 → 全分辨率 PIL RGB 图，不缩放不裁剪。供两遍放大用。
        ⚠️ 原来这行写着「（masked）」—— 那是遮罩时代的残留，已删（见下）。
        ⛔ 原来有个 mask_rect 参数（把 Nano 自己那块涂黑）——2026-08-23 整个删了，
        理由见 _look_at_screen_impl 里那段。**改名以免下一个人以为它还在遮罩。**
        """
        from PIL import Image, ImageDraw
        im = None
        off_x = off_y = 0
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                off_x, off_y = mon["left"], mon["top"]
                shot = sct.grab(mon)
                im = Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            try:
                from PIL import ImageGrab
                im = ImageGrab.grab()
            except Exception:
                return None
        if im is None:
            return None
        im = im.convert("RGB")
        # ⛔ [2026-08-23 已定：整个删掉] 这里原来把 Nano 自己那块**涂黑**。
        #
        # 🔴 它是个**早期产物，而且是错的**：那张图**既给模型看、也给用户看**，
        #    是同一张 —— 所以涂黑不是「把自己排除掉」，是**把那块屏幕的信息
        #    对谁都抹掉了**（Nano 窗口下面盖着的东西也一起没了）。
        # 📌 **一个「排除自己」的实现，如果连自己也看不见它排除掉的东西，
        #    那它排除的就不是自己，是那块区域。**
        # ⚠️ 顺带：涂黑还让截图很难看（整块死黑），而它换来的收益是零。
        # ⭐ 「先缩小自己」这件事**照旧要做**，但理由换成真的那个：
        #    **Nano 挡着你要看的东西** —— 而不是「不然会被涂黑三分之一」。
        return im

    @staticmethod
    def _pil_to_png(im, max_w: int = 1568) -> bytes:
        """PIL 图 → PNG bytes。宽于 max_w 才缩；小裁剪会被放大到 max_w（小字变清晰）。"""
        import io
        from PIL import Image
        if im.width != max_w:
            r = max_w / im.width
            im = im.resize((max_w, max(1, int(im.height * r))), Image.LANCZOS)
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

    @staticmethod
    def _parse_loose_json(s: str):
        import json, re
        if not s:
            return None
        try:
            return json.loads(s)
        except Exception:
            pass
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
        return None

    async def _vision_ask(self, png: bytes, prompt: str, system: str) -> str:
        """把一张图 + 提问发给视觉模型，返回文字。Anthropic content 格式。"""
        # ⚠️ 走**视觉槽**而不是主模型 —— 主模型未必有视觉能力
        #    （深度求索只有 flash-vision-exp 有）。
        img_part = self.provider.build_image_part(png, "image/png")
        _vm = _vision_model_for_os()
        context = [{"role": "user", "content": [{"type": "text", "text": prompt}, img_part]}]
        content, _ = await self.provider.chat_without_tools(
            context, system, model_override=_vm)
        return (content or "").strip()

    async def _seed_image_summary_via_vision(self, image_parts: list, query: str):
        """主模型没视觉时：视觉槽先看一眼，结果直接写进 `image_summary`。

        返回**还要发给主模型的 image_parts** —— 成功则 `None`（pixels 不发，
        免得盲主模型读到 `[Unsupported Image]` 然后诚实报告"我看不到"），
        失败则原样返回（退回老路）。
        ⚠️ 降级绝不抛：它不该有能力把主流程带走。
        """
        try:
            from core.runtime.blobs import extract_image_blocks
            pairs = extract_image_blocks(image_parts)
            if not pairs:
                return image_parts
            raw, _mime = pairs[0]
            # ⭐ 带上用户这轮的问题 —— 比通用描述准，且不额外花钱。
            #    但仍要求写成**独立摘要**：这段文字要留给以后的自己，
            #    不能写成"针对这一问的答案"（同 note_image 的那条约束）。
            ask = (
                "Describe this image for your own later reference, independently of any "
                "question: what it is, its layout, and the text / objects / colors / counts "
                "that are actually visible. Be concrete and dense." + _NL
                + "The user's message this turn was: "
                + (query or "").strip()[:400] + _NL
                + "Make sure anything relevant to that is covered, but do NOT answer it here."
            )
            desc = await self._vision_ask(raw, ask, "You are a precise visual describer.")
            if not (desc or "").strip():
                return image_parts
            self.memory.set_image_summary(desc.strip())
            # 摘要已经有了 ⇒ 别再给 note_image（它会拿主模型的盲视覆盖掉这份）
            self._turn_image_pending = False
            # 🔴 **绝不能返回 None** —— 注入块（`if temp_file_hint or image_parts:`）
            #    是模型知道"这一轮来了张图"的**唯一渠道**；置空它，模型的上下文里
            #    一个字都不会提到图，然后它会满机器去找图（实测，52s / 12.7K tok）。
            # ⇒ 把 pixels **换成文字块**，走同一条注入线。
            # 🔴 这里必须用 `short_handle()` —— `ui_images` 存的是**完整 ref**
            #    （`6cd3f9d4….png`），直接当 handle 给模型，`resolve_handle` 的
            #    hex 校验一看见 `.png` 就判死。
            #    📌 **一个有专用构造函数的标识符，永远别自己拼。**
            _handles = []
            try:
                from core.runtime.blobs import short_handle as _sh
                _m = self.memory._last_user_image_message()
                _refs = list(getattr(_m, "ui_images", None) or []) if _m else []
                _handles = [h for h in (_sh(r) for r in _refs) if h]
            except Exception:
                pass
            _hint = (" You can call view_past_image with handle "
                     + str(_handles[0]) + " to look again for details this text omits."
                     ) if _handles else ""
            logger.info(f"[D10] 主模型无视觉 -> 视觉槽代看，摘要 {len(desc)} 字")
            return [{"type": "text",
                     "text": ("[The user attached an image to this message. Your model cannot "
                              "see pixels, so it was read for you by the vision model. "
                              "What it shows:]" + _NL + desc.strip() + _NL + _hint)}]
        except Exception as e:
            # ⚠️ 留痕：静默退回会让这条 bug 长得跟"主模型有视觉"一模一样。
            logger.warning(f"[D10] 视觉槽代看失败，退回原路（pixels 照发）: {e}")
            return image_parts

    async def _emit_shot(self, event_queue, png: bytes, purpose: str):
        """把截到/放大的图推进聊天流（用户看见 Nano 看到了什么）。"""
        if event_queue is None:
            return
        try:
            import base64 as _b64
            await event_queue.put({
                "event": "screenshot_preview",
                "png_b64": _b64.b64encode(png).decode(),
                "purpose": purpose,
            })
        except Exception:
            pass

    def _window_identity_note(self) -> str:
        """⭐ 每次看屏幕都如实告诉模型**它在对着哪个窗口**，以及有没有换过。

        ⚠️⚠️ 这个方法来自一次**真实的数据损坏**：
        Nano 要操作自己打开的 `新建文本文档.txt`，用户中途把焦点放到了**自己的**
        另一个记事本上。`get_target_window()` 返回的是"最前面那个非 Nano 窗口"，
        于是 Nano 对着**用户的**记事本 Ctrl+A + 输入，**把用户的内容清掉了**。

        当时 `look_at_screen` 只返回视觉模型的散文描述，**一个字都没提在看哪个窗口**，
        模型除了在图里认标题之外没有任何手段发现自己换了对象。

        📌 **判据：不要让模型去「记得怀疑」，要把变化本身摆到它眼前。**
        早先的设计 三层防护网第 1 层（"提示词写明挂起前后环境不能默认一致"）
        要求的是模型**自律**；这里给的是**事实**。两者不是重复 ——
        一条是"你应该怀疑"，一条是"这就是变了"。

        ⚠️ 身份用 **hwnd**，不是标题：两个未命名记事本的标题**完全一样**
        （都是"无标题 - 记事本"），靠标题判身份正好在最该分清的场景下失效。

        ⚠️ 比较基准是**本轮**（`_last_fg_window` 每轮重置），与活动租约同一个边界 ——
        跨轮的"变了"没有意义，那本来就是两件事之间。
        """
        try:
            from core.os_layer.executor_low import foreground_identity
            cur = foreground_identity()
        except Exception:
            return ""
        if not cur:
            return ""
        prev = getattr(self, "_last_fg_window", None)
        self._last_fg_window = cur
        _desc = f"\"{cur['title']}\"" + (f" ({cur['proc']})" if cur['proc'] else "")
        head = f"[Foreground window] hwnd={cur['hwnd']} {_desc}"

        # ⭐⭐ 比"前台是谁"重要得多的一件事：**我的目标窗口现在怎么样了。**
        #    前台变了只说明"有别的窗口上来了"；而"目标窗口还在不在、是不是被最小化了、
        #    坐标还有效吗"才是决定下一步动作的信息 —— 而且**截图答不了这些**
        #    （关了/最小化/被挡/在别的显示器，截图看起来全都一样）。
        try:
            from core.os_layer import window_binding as _wb
            _lid = (getattr(self, "_rt_os_lease", None) or ("", 0))[0]
            _tgt = _wb.describe(_lid) or _wb.gone_note(_lid)
            if _tgt:
                head = _tgt + "\n" + head
        except Exception:
            pass

        if prev and prev.get("hwnd") != cur["hwnd"]:
            head += (
                f"\n⚠️ The foreground window CHANGED since your last look "
                f"(was hwnd={prev['hwnd']} \"{prev.get('title','')}\").\n"
                "⚠️ A window that looks like the one you were working on may be a "
                "DIFFERENT window — identical titles are common (e.g. two untitled "
                "Notepad windows). Do NOT assume this is the file you opened.\n"
                "Before any destructive action (select-all, delete, overwrite, save), "
                "confirm this is really your target. If you cannot confirm it, stop and ask."
            )
        return head

    async def _look_at_screen(self, purpose: str, include_self: bool,
                              event_queue=None, region: str = "full") -> str:
        """截屏 + 视觉理解，**并在结果最前面附上前台窗口的真实身份**。

        ⭐ 窗口身份来自 Win32（`GetForegroundWindow`），**不是**问视觉模型 ——
        它是事实而不是推断。视觉模型连"这是哪个窗口"都没被问过，
        更不该由它来回答这种能造成破坏性误操作的问题。
        """
        note = self._window_identity_note()
        body = await self._look_at_screen_impl(purpose, include_self, event_queue, region)
        return f"{note}\n\n{body}" if note else body

    async def _look_at_screen_impl(self, purpose: str, include_self: bool,
                                   event_queue=None, region: str = "full") -> str:
        """截屏 + 视觉理解。默认【自动两遍放大】——整屏看一遍，看不清的小目标由
        视觉模型自己指出大概位置，工具自动裁那块放大到全清晰再看一遍，主模型无需选区域。
        region != full 时为手动覆盖（直接裁那块单遍看）。
        """
        if self.provider is None:
            return "（无法截图自查：视觉模型不可用）"
        SYS = "You are Nano's eyes. Describe only what is actually visible on the screen to help decide the next step. If unclear, say it is unclear."
        # ── 让开自己：**只剩一条路 —— 真的最小化**──
        #
        # ⛔ 这里原来有两条：小窗(mini) → **涂黑**；大窗 → 最小化。
        #    涂黑那条整个删了，因为它是**早期产物而且是错的**：
        #    🔴 那张图**既给模型看、也给用户看**，是同一张 ——
        #       涂黑不是「排除自己」，是**把那块屏幕对谁都抹掉**
        #       （Nano 窗口下面盖着的东西一起没了）。
        #    📌 **一个「排除自己」的实现，如果连自己也看不见它排除掉的东西，
        #       那它排除的就不是自己，是那块区域。**
        #
        # ⭐ 而「最小化再恢复」是**真的让开** —— 它保留下来，且现在无条件走。
        # ⚠️ 顺带删掉了 `include_self` 参数：遮罩没了，它就没有对象了。
        #    📌 一个没有实现的参数留着，模型会拿它当真的用。
        # ⚠️ `include_self=True` 时**什么都不做** —— 那正是「我要看自己」的唯一走法。
        _win = None
        if not include_self:
            try:
                _win = self._native_window() if self._native_window else None
                if _win is not None:
                    _win.minimize()
                    await asyncio.sleep(0.45)
            except Exception:
                _win = None
        try:
            im = await asyncio.to_thread(self._capture_screen_image)
        finally:
            if _win is not None:
                try:
                    _win.restore()
                except Exception:
                    pass
        if im is None:
            return "（截图失败，没看到屏幕）"

        # ── 手动 region 覆盖：直接裁那块、单遍看 ──────────────────────────
        if region in self._LOOK_FRACS:
            fr = self._LOOK_FRACS[region]; w, h = im.size
            crop = im.crop((int(fr[0]*w), int(fr[1]*h), int(fr[2]*w), int(fr[3]*h)))
            png = self._pil_to_png(crop)
            await self._emit_shot(event_queue, png, purpose)
            try:
                ans = await self._vision_ask(
                    png,
                    f"This is a zoomed region of the screen. Need: {purpose}\n"
                    + self._LOOK_ANSWER_RULES, SYS)
                return ans or "（视觉模型没有返回内容）"
            except Exception as e:
                return f"（视觉分析失败：{e}）"

        # ── 自动两遍放大 ────────────────────────────────────────────────
        try:
            overview = self._pil_to_png(im, 1568)
            pass1 = (
                f"This is the current computer screenshot. Need: {purpose}\n"
                + self._LOOK_ANSWER_RULES +
                "If the target may be visible but too small or blurry "
                "(such as a name, filename, list item, or small icon), do not treat it as absent. "
                "Instead set need_zoom and return zoom_bbox for the larger surrounding area to "
                "zoom into; that box should include context and may be larger than the target.\n"
                'Return strict JSON only: {"answer":"...", '
                '"targets":[{"name":"...","box":[x0,y0,x1,y1]}], '
                '"need_zoom":true/false, "zoom_bbox":[x0,y0,x1,y1] or null}. '
                "All boxes use 0-1 normalized coordinates with top-left as (0,0)."
            )
            raw1 = await self._vision_ask(overview, pass1, "You are Nano's eyes. Output only the required JSON.")
            data = self._parse_loose_json(raw1)
            self._look_diag("pass1(整屏)", purpose, raw1, data)
            if not data or not data.get("need_zoom") or not data.get("zoom_bbox"):
                await self._emit_shot(event_queue, overview, purpose)
                _ans = ((data or {}).get("answer") or raw1) or "（视觉模型没有返回内容）"
                return _ans + self._targets_note(data, im.size)

            # 需要放大：从【全分辨率】原图裁 zoom_bbox → 放大 → 第二遍精确看。
            # ★ 它是 LLM 估的、本来就糙，所以【以中心扩张、保证足够大】——
            #   宁可裁大点带上下文，也别裁太紧框歪了漏掉目标（"找错太多次"的根因）。
            # ⚠️ 2026-08-24 由 `bbox` 更名为 `zoom_bbox`：这一版新增了 `targets`
            #    （目标本身的框），📌 两个都叫 box 而语义相反（一个是「放大哪」、
            #    一个是「目标在哪」）—— 不改名迟早有人读错一个。
            x0, y0, x1, y1 = [float(v) for v in data["zoom_bbox"]]
            w, h = im.size
            cxc = (min(x0, x1) + max(x0, x1)) / 2.0
            cyc = (min(y0, y1) + max(y0, y1)) / 2.0
            half_w = max((max(x0, x1) - min(x0, x1)) / 2.0 + 0.06, 0.20)  # 至少 ~40% 宽
            half_h = max((max(y0, y1) - min(y0, y1)) / 2.0 + 0.06, 0.18)  # 至少 ~36% 高
            fx0 = max(0.0, cxc - half_w); fy0 = max(0.0, cyc - half_h)
            fx1 = min(1.0, cxc + half_w); fy1 = min(1.0, cyc + half_h)
            cx0, cy0, cx1, cy1 = int(fx0*w), int(fy0*h), int(fx1*w), int(fy1*h)
            if cx1 - cx0 < 20 or cy1 - cy0 < 20:
                await self._emit_shot(event_queue, overview, purpose)
                return (data.get("answer") or "（看了一眼，没能精确定位）") \
                    + self._targets_note(data, im.size)
            crop = im.crop((cx0, cy0, cx1, cy1))
            zoom_png = self._pil_to_png(crop, 1568)   # 裁剪放大到 1568 → 小字清晰
            await self._emit_shot(event_queue, zoom_png, purpose)
            raw2 = await self._vision_ask(
                zoom_png,
                f"This is a zoomed region from the previous screenshot. Need: {purpose}\n"
                + self._LOOK_ANSWER_RULES +
                "Do not treat similar-looking items as the target. Clearly say whether each "
                "item is present. If this region does not contain it, say so.\n"
                'Return strict JSON only: {"answer":"...", '
                '"targets":[{"name":"...","box":[x0,y0,x1,y1]}]}. '
                "Boxes are 0-1 normalized coordinates of THIS zoomed image.",
                "You are Nano's eyes. Output only the required JSON.",
            )
            d2 = self._parse_loose_json(raw2)
            self._look_diag("pass2(放大)", purpose, raw2, d2)
            # ⚠️ 第二遍的框在**裁剪图**的坐标系里 → 换算回全屏。
            #    偏移 + 缩放，两个都是我们自己算的，精确可逆。
            _note2 = self._targets_note(d2, (cx1 - cx0, cy1 - cy0), offset=(cx0, cy0))
            # ⭐⭐⭐ [2026-08-25 实测] **两遍的答案要一起交出去，不能后一遍盖掉前一遍。**
            #
            # 🔴 问题：这里原来只返回 `_ans2`（放大那一遍），把整屏那一遍的答案**丢掉**。
            #    实测：模型放大的那块**没盖住**屏幕最下面那条新消息，
            #    于是它只拿到「这一块里没有」，得出结论「窗口需要下拉」——
            #    而整屏那一遍其实已经看见了。
            # 📌 **放大是为了看清，不是为了缩小搜索范围。**
            #    一次放大之后「没看到」，只说明**那一块里没有**，
            #    不能当成「屏幕上没有」—— 而旧写法把这两句话变成了同一句。
            # ⚠️ 所以不光要合并，还要**把作用域说出来**：
            #    📌 一个不标明取景范围的观察结果，会被当成对整个画面的断言。
            _ans1 = ((data or {}).get("answer") or "").strip()
            _ans2 = ((d2 or {}).get("answer") or raw2 or "").strip()
            if not _ans2:
                return (_ans1 or "（放大后仍看不清）") + _note2
            _parts = []
            if _ans1:
                _parts.append(f"[Whole screen] {_ans1}")
            _parts.append(f"[Zoomed into region {cx0},{cy0}-{cx1},{cy1}] {_ans2}")
            _parts.append(
                "NOTE: the zoomed view covers only that rectangle. If what you were "
                "looking for is not in it, that is NOT evidence it is absent from the "
                "screen - the whole-screen line above is the wider view. Do not conclude "
                "you need to scroll just because the zoom missed it."
            )
            return "\n".join(_parts) + _note2
        except Exception as e:
            return f"（视觉分析失败：{e}）"

    # ⭐⭐⭐ [2026-08-24] **看完了要把坐标交出来。**
    #
    # 🔴 问题：`look_at_screen` 只返回**散文**（「发送按钮在右侧，是个纸飞机图标」）。
    #    pass1 确实算过一个 box，但那个 box 的语义是「放大哪一块」，用完就丢。
    #    ⇒ Nano **从来没拿到过任何坐标**。它下一步要点，只能
    #      `click(target="发送按钮")` —— 走整条 locate 链再赌一次定位。
    # 🔴 而 Nano 自己复盘时说「我本应直接点击那个坐标」——
    #    📌 **它描述的是一个它没有的能力。** 一个模型对自己能力的自述不能当证据用。
    # ⭐ 真正的账不是「省一次 locate」：
    #      现在：每一次点击都在**重新赌一次定位**（那条链跑了 8 次错了大半）
    #      改后：一次勘察拿全坐标 → 后面几步直接点 → **那条链只被走一次**
    #    ⚠️ 它**不改善单次定位的精度** —— 只把赌的次数从 N 降到 1。
    _LOOK_ANSWER_RULES = (
        "Answer every item asked for in the request - not only the first one. "
        "For each item you can see, give its bounding box so it can be clicked "
        "later without looking again. Be truthful; if something is unclear or "
        "absent, say so instead of guessing.\n"
    )

    # ⭐⭐ [2026-08-24] **给 `look_at_screen` 也加上诊断。**
    #
    # 🔴 实测：坐标那一段（`[Screen coordinates from this look …]`）**一次都没出现**，
    #    而**分不出**是哪一种：
    #      A 模型压根没填 `targets`（没按新 JSON 格式答）
    #      B 填了，但 `_parse_loose_json` 没解析出来 → data 是 None → 返回空串
    # 📌 而这是同一晚**第二次**犯同一个疏漏：locate 那条链偏了六轮之后才加
    #    `_diag`，加完一次就定位了根因；然后改 `look_at_screen` 时**又没给它加**。
    #    📌 **一个只记录结果、不记录输入的流程，出了偏差就只能靠猜**
    #       —— 这句就写在 `_diag` 的注释里，然后没照做。
    # ⚠️ 整段吞异常：它是诊断，📌 一个用来看清楚的东西不许成为失败源。
    @staticmethod
    def _look_diag(tag: str, purpose: str, raw, data) -> None:
        try:
            if data is None:
                _snip = (str(raw) or "")[:220].replace("\n", " ")
                logger.warning(f"[Look-Diag] {tag}「{purpose}」 **JSON 没解析出来**"
                               f"（B 类）| 原文前 220 字：{_snip}")
                return
            _keys = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
            _t = (data or {}).get("targets")
            if not _t:
                _snip = (str(raw) or "")[:220].replace("\n", " ")
                logger.warning(f"[Look-Diag] {tag}「{purpose}」 解析成功但 "
                               f"**targets 为空**（A 类）| keys={_keys} | "
                               f"原文前 220 字：{_snip}")
                return
            logger.info(f"[Look-Diag] {tag}「{purpose}」 keys={_keys} | "
                        f"targets 原始={_t}")
        except Exception:
            pass

    @staticmethod
    def _targets_note(data, size, offset=(0, 0)) -> str:
        """把模型给的 `targets`（归一化框）换算成**屏幕坐标**，附在回答后面。

        ⚠️ `size` 是**那张图对应的屏幕区域**大小，`offset` 是它的左上角 ——
           整屏那一遍 offset=(0,0)；放大那一遍是裁剪区的左上角。
           📌 一个换算函数如果自己去猜「这是第几遍」，它就知道了不该知道的事；
              让调用方把区域交进来，两遍共用同一份实现。
        ⚠️ 失败返回空串：📌 坐标是**附加**信息，拿不到不该让整次「看屏幕」失败。
        """
        try:
            items = (data or {}).get("targets") or []
            if not items:
                return ""
            _w, _h = int(size[0]), int(size[1])
            _ox, _oy = int(offset[0]), int(offset[1])
            lines = []
            for it in items[:8]:
                box = it.get("box")
                if not box or len(box) != 4:
                    continue
                x0b, x1b = sorted((float(box[0]), float(box[2])))
                y0b, y1b = sorted((float(box[1]), float(box[3])))
                cx = _ox + int((x0b + x1b) / 2 * _w)
                cy = _oy + int((y0b + y1b) / 2 * _h)
                lines.append(f"  - {it.get('name') or '?'}: x={cx}, y={cy}")
            if not lines:
                logger.warning("[Look-Diag] targets 有内容但一个 box 都没解析出来")
                return ""
            logger.info(f"[Look-Diag] 换算后屏幕坐标（区域 {_w}x{_h} @ "
                        f"{_ox},{_oy}）：{lines}")
            return ("\n\n[Screen coordinates from this look — click them directly with "
                    "computer_use click(x=…, y=…); do NOT look again just to find them]\n"
                    + "\n".join(lines))
        except Exception:
            return ""

    async def _handle_look_at_screen(self, args: dict, aid: str, *,
                                     event_queue, **_ctx) -> str:
        # 截图自查：截屏 → 视觉模型理解 → 返回描述。默认排除 Nano 自己窗口。
        _purpose = (args.get("purpose") or "").strip() or "understand the current screen state"
        _region = (args.get("region") or "full").strip().lower()
        _include_self = bool(args.get("include_self", False))
        return await self._look_at_screen(_purpose, _include_self, event_queue, _region)

    async def _handle_note_image(self, args: dict, aid: str, *,
                                 event_queue, **_ctx) -> str:
        """把模型当轮写下的图片描述挂到那条 user 消息上。

        ⚠️ 落到**账本 + storage 两处**：storage 让本轮之后的压缩能用上它，
           落盘让重启后 `_restore_image_notes` 还算得出来。
        ⚠️ 写完之后 `has_unsummarized_image()` 立刻为假 → 工具和动态段**一起消失**。
           📌 这就是"不会每轮注入"的**结构性**保证，不是靠谁记得。
        """
        _s = (args.get("summary") or "").strip()
        if not _s:
            return "Nothing was written down — summary was empty. Try again with the actual content."
        _n = self.memory.set_image_summary(_s)
        # ⚠️ 标志无论如何都要落下 —— 哪怕落库失败。
        #    📌 否则「记不下来」会变成「每一轮都再要求它记一次」，
        #       而那正是 已明确不许出现的形状（像 base64 那个 bug 一样每轮注入）。
        self._turn_image_pending = False
        if not _n:
            return ("There is no image on the current turn to write down, so nothing was saved. "
                    "Just answer the user.")
        return ("Noted. This is stored for your future self only — now answer the user's actual "
                "message in your own voice. Do NOT recite the summary back to them.")

    async def _handle_view_past_image(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        """把盘上那张原图重新喂给视觉模型，返回**文字**。

        ⭐ 与 `look_at_screen` 同一条通路（`_vision_ask`），刻意不发明第二套：
           那边早就解决了"图怎么进视觉模型、结果怎么变成 tool_result"。

        ⚠️ **不把像素塞回主上下文** —— 那正好抵消了压缩，而且每次回看都要再付一遍。
           📌 回看的产物是**一句关于那个细节的回答**，不是"把图重新挂回历史"。

        ⚠️ 也**不发 `screenshot_preview` 上屏**：用户此刻正看着自己发的那张图
           （之后它一直在 UI 里），再画一遍纯属重复。
           📌 与 `look_at_screen` 的差别正在这里 —— 那张图用户**没见过**。
        """
        _h = (args.get("handle") or "").strip()
        _q = (args.get("question") or "").strip() or "Describe what is in this image."
        try:
            from core.runtime.blobs import resolve_handle, image_path
        except Exception as _e:
            return f"(Cannot look at past images right now: {_e})"
        _ref = resolve_handle(_h)
        if not _ref:
            # ⚠️ 认不出**必须响亮地说**，不许含糊成"图没了" —— 前者用户能改（换个把手），
            #    后者会让模型转头去要求用户重新上传，正是 一开始那个 bug。
            return (
                f"No stored image matches handle {_h!r}. Handles look like img#a3f2c1d4 and "
                "appear in the system note on the message that carried the image. "
                "Do not guess, and do not tell the user the image is gone — say you could not "
                "resolve that handle."
            )
        _p = image_path(_ref)
        if _p is None:
            # 🔴 真的没了（用户清了 data/chat_images/）。**这时才轮到请用户重发。**
            return (
                "That image was stored earlier but its file is no longer on disk (the image "
                "library may have been cleared). You still saw it at the time — answer from what "
                "you already know if you can. If you truly need the pixels, now it IS correct to "
                "ask the user to send it again, and say why."
            )
        if self.provider is None:
            return "(The vision model is unavailable, so the image cannot be re-examined.)"
        try:
            _png = await asyncio.to_thread(_p.read_bytes)
            _ans = await self._vision_ask(
                _png,
                f"This is an image the user sent earlier in the conversation. Need: {_q}\n"
                "Answer briefly and truthfully. If the detail cannot be determined from the "
                "image, say so plainly instead of guessing — and if it is a count that is only "
                "approximable, say it is approximate.",
                "You are Nano's eyes. Describe only what is actually visible in this image.",
            )
            logger.info(f"[D10] 回看 {_ref[:8]} → {_q[:40]}")
            return _ans or "(The vision model returned nothing.)"
        except Exception as e:
            return f"(Failed to re-examine that image: {e})"

    async def _handle_set_window_mode(self, args: dict, aid: str, *,
                                      event_queue, **_ctx) -> str:
        # 把窗口切到 mini / full。窗口缩放+布局 reflow 在 UI 层做。
        _wm_mode = (args.get("mode") or "").strip().lower()
        if _wm_mode not in ("mini", "full"):
            _wm_mode = "mini"
        # 窗口形态与「GUI 任务」分开：
        #   · GUI 任务 = GUI 会话租约 + 临时免确认授权，由后端开始 / 结束；
        #   · 窗口形态（mini / full）只是呈现，用户手动放大不结束任务。
        # 结束点：这里的 full、非挂起的轮结束（turn.py）、重启收尾。
        if _wm_mode == "full":
            self._gui_task_end("set_window_mode('full')")
            await event_queue.put({"event": "window_mode", "mode": "full"})
            return ("The screen-operation task has ended: the temporary automatic authorization "
                    "is revoked and Nano's window has been restored to full mode.")

        # 已经是 mini：拒绝重复缩窗（不执行，并说清现状）。
        if self._window_mode_now() == "mini":
            logger.info("[Window] 已经是 mini，拒绝重复的 set_window_mode('mini')")
            return ToolOutcome(
                "Nano's window is ALREADY in mini mode - this call was rejected and "
                "nothing happened. You do not need to shrink again; the window stays "
                "mini for the rest of this task. Go straight to what you wanted to do.",
                True,
            )

        # GUI 任务仍在（用户中途放大了窗口）：同一个任务，不重新授权，直接缩回。
        if self._gui_task_active():
            self._set_window_mode("mini")
            self._window_note = ""
            await event_queue.put({"event": "window_mode", "mode": "mini"})
            return (
                "Nano's window is mini again. The screen-operation task was still active, so "
                "no new authorization was needed. The user had enlarged the window earlier: "
                "tell them in one short sentence why you shrank it (what it was blocking), "
                "then continue the task - do not stop to ask."
            )

        # 没有 GUI 任务：缩窗前先请用户授权本次任务（全局 auto 已开时界面直接放行）。
        # 授权在缩窗前弹：那时目标窗口没有瞬态 UI 可丢，点授权偷焦点也无害。
        _ev = asyncio.Event()
        _approved = [False]
        _loop = asyncio.get_running_loop()

        def _on_approve():
            _approved[0] = True
            _loop.call_soon_threadsafe(_ev.set)

        def _on_reject():
            _approved[0] = False
            _loop.call_soon_threadsafe(_ev.set)

        from core.runtime import replies as _replies
        _rid = _replies.register({"approve": _on_approve, "reject": _on_reject})
        await event_queue.put({
            "event": "mini_auth_request",
            "reply_id": _rid, "actions": ["approve", "reject"],
        })
        from core.runtime import inbox as _ib6
        # 用户打字打断授权请求按「不授权」处理。
        try:
            _oc_mini = await _ib6.wait_confirm_or_user_message(_ev, 300)
        finally:
            _replies.discard(_rid)
        if _oc_mini != _ib6.ConfirmOutcome.CONFIRMED:
            _approved[0] = False
        if _approved[0]:
            self._gui_task_begin("set_window_mode('mini') approved")
            self._set_window_mode("mini")
            return (
                "Nano has been minimized to the top-right mini window and the user authorized automatic screen operation for this task. "
                "You may now continue operating the user's screen without repeated confirmation prompts. "
                "IMPORTANT: when the screen work is done, call set_window_mode('full'). That call is what ends the task and "
                "revokes this temporary authorization. The task does NOT end with the turn; if you forget, the authorization "
                "stays active and your window stays small until the task times out after 15 idle minutes."
            )
        return (
            "The user rejected this screen-operation authorization or the request timed out. "
            "Do not operate the screen again. Tell the user that authorization is needed to continue, or suggest completing it manually."
        )

    # ── GUI 任务与窗口形态 ────────────────────────────────────────────

    def _window_mode_now(self) -> str:
        """后端记录的窗口形态：'mini' / 'full'（启动时为 full）。"""
        return getattr(self, "_window_mode", "full")

    def _set_window_mode(self, mode: str) -> None:
        self._window_mode = "mini" if mode == "mini" else "full"

    @staticmethod
    def _gui_task_active() -> bool:
        """GUI 任务是否进行中（以 GUI 会话租约为准；读不到按「没有」处理）。"""
        try:
            from core.runtime import oslease as _ol
            from core.runtime.kernel import get_kernel as _gk
            return bool(_ol.gui_session_active(_gk()))
        except Exception:
            return False

    def _gui_task_begin(self, reason: str) -> None:
        """开始 GUI 任务：先开 GUI 会话（建 Task），再发绑定到该 Task 的临时免确认授权。"""
        try:
            from core.runtime import oslease as _ol
            _ol.open_gui_session(reason)
            _ol.grant_temp_auto(reason)
        except Exception as e:
            logger.warning(f"[Window] 开始 GUI 任务失败: {e}")
        self._gui_task_touch()

    def _gui_task_end(self, reason: str) -> None:
        """结束 GUI 任务：收回临时免确认授权、关 GUI 会话；窗口形态记为 full。幂等。"""
        try:
            from core.runtime import oslease as _ol
            if self._gui_task_active():
                logger.info(f"[Window] GUI 任务结束（{reason}）")
            _ol.revoke_temp_auto()
            _ol.close_gui_session()
        except Exception as e:
            logger.warning(f"[Window] 结束 GUI 任务失败（授权可能仍有效）: {e}")
        self._set_window_mode("full")
        self._window_note = ""

    def notify_window_enlarged_by_user(self) -> None:
        """界面报告：用户手动把窗口放大了。只改窗口形态，GUI 任务与授权不变；
        下一个工具结果会附上一段说明。"""
        self._set_window_mode("full")
        if self._gui_task_active():
            self._window_note = (
                "[Window] The user just enlarged Nano's window while this screen-operation "
                "task is running. They want the window large - respect that. The task and its "
                "authorization are unchanged. Before each next step, ask yourself whether the "
                "window size really gets in the way: a drag across the area it covers, or a "
                "click / typing target hidden under it. Looking at the screen is never a reason "
                "(look_at_screen hides Nano by itself). Only if it truly blocks the next step, "
                "call set_window_mode('mini') and say in one short sentence why; then continue "
                "without stopping to ask.")

    def _gui_task_touch(self) -> None:
        """记一次屏幕相关活动（空闲兜底从这里起算）。"""
        self._gui_last_activity = time.monotonic()

    async def _gui_task_track_turn(self, events):
        """包住一轮 ReAct 的事件流：记录「轮进行中 / 本轮是否以挂起结束」，
        用户按停止时结束 GUI 任务。GUI 任务本身不随轮结束。"""
        suspended = False
        self._turn_running = True
        try:
            async for ev in events:
                if isinstance(ev, dict):
                    kind = ev.get("event")
                    if kind == "suspend_waiting":
                        suspended = True
                    elif kind == "turn_interrupted" and ev.get("stopped"):
                        self._gui_task_end("user stopped the turn")
                yield ev
        finally:
            self._turn_running = False
            self._last_turn_suspended = suspended
            # 轮结束也算一次活动：空闲从轮结束时起算，而不是从最后一个屏幕动作起算。
            if self._gui_task_active():
                self._gui_task_touch()

    def _gui_task_idle_check(self, now: float | None = None) -> bool:
        """GUI 任务的空闲兜底：没有进行中的轮、没有等待唤醒的挂起、且空闲满
        `_GUI_TASK_IDLE_SEC`，就结束任务（界面随后恢复窗口）。返回是否结束了任务。"""
        if not self._gui_task_active():
            return False
        if getattr(self, "_turn_running", False):
            return False
        if getattr(self, "_last_turn_suspended", False) and self._live_suspension_exists():
            return False
        last = getattr(self, "_gui_last_activity", None)
        now = time.monotonic() if now is None else now
        if last is None:
            # 进程内没有记录（任务由别处开始）：从第一次检查起算。
            self._gui_last_activity = now
            return False
        if now - last < self._GUI_TASK_IDLE_SEC:
            return False
        self._gui_task_end(f"idle for {int(now - last)}s")
        return True

    @staticmethod
    def _live_suspension_exists() -> bool:
        try:
            from core.runtime import waitcond as _wc
            from core.runtime.kernel import get_kernel as _gk
            return bool(_wc.list_live(_gk()))
        except Exception:
            return False

    def _install_gui_task_idle_tick(self) -> None:
        """把空闲兜底登记为 runtime reconcile 的周期步骤（同名登记会覆盖，可重复调用）。"""
        try:
            from core.runtime import reconciler as _rec

            def _tick(_kernel, report) -> None:
                if self._gui_task_idle_check():
                    report.extra["gui_task_idle_ended"] = 1

            _rec.register_tick_step("gui_task_idle", _tick)
        except Exception as e:
            logger.warning(f"[Window] 登记 GUI 任务空闲兜底失败: {e}")

    def _take_window_note(self) -> str:
        """取出并清空待附在工具结果上的窗口说明。"""
        note = getattr(self, "_window_note", "")
        self._window_note = ""
        return note
