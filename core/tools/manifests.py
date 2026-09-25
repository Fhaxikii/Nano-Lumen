# core/tools/manifests.py
"""内置工具的 schema（发给模型的 name / description / parameters）。

每个工具的其余事实（感知文案、卡片、调度、绑定的 handler）在 `builtin.py`；
`build_builtin_definitions(BUILTIN_MANIFESTS)` 按工具名把两边合起来。
新增内置工具：在这里写 manifest 并加进 `BUILTIN_MANIFESTS`，再到 `builtin.py` 声明其余部分。
"""
from core import reading as _READING

_WRITE_SKILL_MANIFEST = {
    "name": "WriteSkill",
    "description": (
        "Use this tool when the user asks to create, add, or develop a new local Skill. "
        "Generate complete Python Skill code according to Nano Skill Development Protocol v3.2 and output it through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Skill class name and filename without .py, such as WeatherQuery."
            },
            "code": {
                "type": "string",
                "description": "Complete Python Skill code that follows Nano Skill Development Protocol v3.2."
            },
            "description": {
                "type": "string",
                "description": (
                    "One-sentence user-facing description shown in the audit/confirmation window. "
                    "Use the same language as the user's Skill request."
                )
            }
        },
        "required": ["filename", "code", "description"]
    }
}


# ── [回看设计 2026-08-09] 「下一次什么时候回来看」──────────────────────────
# ⭐⭐ **系统只设第一次回看，之后全归模型** —— 这就是那个「归模型」的落点。
# 📌 常量只该承担系统答得出的那个问题（「多久之后开始怀疑」）；
#    **「这件事还要多久」只有模型能答**，而任何固定数字都覆盖不了真实情况。
#
# ⚠️ **条件注入**：只在「回看轮」出现（那一轮才有可回看的对象）。
#    常驻会让模型在没有任何后台任务时去调它 —— 同 `task_boundary` 那条：
#    📌 一个只在某种状态下才有意义的工具，应该只在那种状态下出现。
#
# ⚠️ `seconds` 省略 / <=0 → **不再回看**（等完成信号）。刻意不用一个魔数表达它：
#    📌 「不再做这件事」不该靠一个特殊数值表达，那种约定迟早被误用。
_SET_NEXT_CHECKIN_MANIFEST = {
    "name": "set_next_checkin",
    "description": (
        "Decide when (or whether) to look at this background job again.\n"
        "Call this once, after you have judged how it is going.\n"
        "  · seconds = a number  -> look again after that many seconds. "
        "Prefer a LONG gap when it looks healthy: if you expect roughly ten more "
        "minutes, ask for ten minutes, not one. Every check-in costs a full turn.\n"
        "  · seconds omitted     -> do NOT look again; the completion signal will "
        "wake you. Use this when it looks nearly done.\n"
        "If you say nothing at all, a long default is used — so calling this is only "
        "needed when you want something different from that.\n\n"
        "On a check-in, decide first, then give at most one user-facing status conclusion. "
        "If this call adds no new fact, do not repeat the same progress both before and after it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "integer",
                "description": (
                    "Seconds until the next check-in. Omit to stop checking in."
                ),
            },
        },
        "required": [],
    },
}


# ── 「这个调用我不等了」──────────────────────────────────────────────
#
# ⭐⭐⭐ **这是「抛后台」的落点，而它的主语是【一次调用】，不是一件 Task。**
#
# 🔴 早先写的是「主语必须是 Task，别做成模型申报某个调用转后台」。
#    2026-08-1x 推翻了它，理由很硬：**没有办法定义「哪一种 Task 值得
#    进后台」** —— 那是个概念，落不了地。而「这个调用我等不等」是模型此刻
#    真的答得出的问题。
#
# ⭐ 与早先那条约定**不冲突**，两者答的是不同的问题：
#      系统答「**谁在跑**」   —— 事实，不需要模型申报（那条约定管的是这个）
#      模型答「**我还等不等**」—— 取决于「下一步依不依赖它」，只有模型知道
#    📌 一个被否掉的申报，否的是它申报的**内容**，不是「模型不许说话」。
#
# ⚠️⚠️ **`next_step` 必填，而且它就是整个判据。**
#    用户的三种情况，分界线全在这一个参数上：
#      ① pip 完才能改代码（有依赖）→ 下一步就是等它 → **填不出别的** → 不该调
#      ② pip 与改代码无关         → 下一步是「改代码」 → 填得出   → 该调
#      ③ 只有 pip 这一件事         → 下一步就是等它     → 填不出   → 不该调
#    📌 **「不等它」只有在「我有别的事要做」时才成立** ——
#       把那个前提做成一个必须写下来的字段，模型就没法含糊过去。
#    ⭐ 这也是实测抓到的滥用（模型逢长任务就 `dont_wait`）的修法：
#       不是在描述里写「别乱用」，是**让它乱用时无话可填**。
#
# ⚠️ 第二条实测教训：模型会把「回头看看它好没好」当成 `next_step` ——
#    那等于什么都没说（它还是在等它），于是 `dont_wait` 白调一次。
#    → 描述里**显式**告诉它：系统会在那件事结束时叫你，你不需要安排去看它。
#    📌 **一个「必须填别的事」的字段，必须同时说清什么不算「别的事」** ——
#       否则模型会用一个语法上合法、语义上等价于「我还是在等」的答案绕过去。
_DONT_WAIT_MANIFEST = {
    "name": "dont_wait",
    "description": (
        # 🔴🔴 **实测 2026-08-20：模型看得见这个工具，却直接跳过它。**
        #    日志：`dont_wait:1070` 在工具表里，而它转头就调了 `edit_file`。
        #    📌 根因是**上一版描述用模型侧的语言说了一件系统侧的事**：
        #       第一句写的是 "Stop waiting for the slow call" ——
        #       可控制权**已经交还给它了**，它主观上根本没在等，
        #       于是这个动作在它看来什么也不改变，自然跳过。
        #    ⭐ 「等 / 不等」的差别只存在于**系统这一侧**（排不排回看、
        #       进不进抽屉），所以描述必须换成**它能据以行动的后果**：
        #       「别在 60 秒后打断我」。那是它自己在乎的事。
        "Hand a slow call that is already running over to the runtime, so it "
        "stops interrupting you about it.\n"
        "\n"
        "By default, after a slow call is handed back to you, the runtime will "
        "pull you back in about a minute just to look at it again, and keep "
        "doing that until it finishes. Call `dont_wait` and that stops: it "
        "moves into the user's task drawer, and you are woken up once, when it "
        "is actually done.\n"
        "\n"
        "Call it BEFORE you start the other work - otherwise you will be "
        "interrupted in the middle of it.\n"
        "\n"
        "Call it ONLY when you genuinely have other work you can do right now "
        "that does NOT depend on that call's result. If your next move is to "
        "wait for it, check it, or use its result, do NOT call this tool - "
        "those periodic look-ins are exactly what you want then.\n"
        "\n"
        "`next_step` must name that other work. These do NOT count as other "
        "work: 'check on it', 'look at it again', 'wait for it', 'see if it "
        "finished', 'report progress to the user'. The runtime tells you when "
        "it is done - you never have to schedule a look."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "next_step": {
                "type": "string",
                "description": (
                    "One short line naming the OTHER work you are going to do while "
                    "it runs - concrete, and independent of that call's result. "
                    "If you cannot name one, do not call this tool."
                ),
            },
        },
        "required": ["next_step"],
    },
}


# ── 「一件事」的边界工具 ──────────────────────────────────────
# ⭐⭐ **为什么必须有这个工具**：Task 的边界是「一个目标达成或放弃」，
#    而那**只有模型知道** —— 三条各自独立的结论都指向这一点
#    （迭代阅读的 scratchpad / 仅本次 MCP / per-task 成本）。
#    代码能给的只有默认值（复用当前那个），说不出「这件事完了」。
#
# ⚠️⚠️ **「继续当前这件事」刻意【不是】一个动作** —— 那是默认。
#    📌 同（`UNRELATED` 不是工具参数，它等于不调用工具）：
#       **默认不需要动作，只有偏离默认才需要动作。**
#       给「继续」一个参数，等于每轮都要模型表态一次，纯烧 token 还多一次犯错机会。
#
# ⚠️ **两个动作分开，不许捆成一个** ——「另开一件事」默认**不结束**旧的。
#    早先的设计原话：「我们的**并不要求 x 和 y 一定相关**」——
#    用户在装库跑着时插一句「先做别的」，那两件事**并存**。
#    📌 捆死之后，模型想表达「并存」就没有说法了。
_TASK_BOUNDARY_MANIFEST = {
    "name": "task_boundary",
    "description": (
        "Declare that a piece of work is finished, or that the user's new request is a "
        "DIFFERENT piece of work.\n"
        "Nano groups long-lived things (timed waits, queued messages, one-off "
        "authorizations, per-task cost) under 'a piece of work' that can span many "
        "turns. Only you can tell when one is done.\n"
        "\n"
        "Call it ONLY when something changes:\n"
        "  · finish  — that work is complete, or the user dropped it\n"
        "  · start   — the user's request is a different piece of work "
        "(the current one is set aside automatically, NOT finished)\n"
        "  · resume  — go back to one that was set aside "
        "(needs its `task_id`)\n"
        "\n"
        "Setting one aside never stops anything: background jobs, timed waits "
        "and agents under it keep running. It only means YOU are not on it now.\n"
        "This tool is about WHICH PIECE OF WORK you are on. It is NOT how you "
        "stop waiting for a slow call - that is `dont_wait`.\n"
        "Do NOT call it to say you are continuing — that is the default. "
        "Do NOT call it for ordinary chat that owns nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["finish", "start", "resume"],
                "description": (
                    "finish = the current piece of work ended; "
                    "start = begin a different one (the current one is set aside "
                    "automatically); "
                    "resume = go back to one that was set aside (give its task_id)."
                ),
            },
            "task_id": {
                "type": "string",
                "description": (
                    "For resume only: the id of the piece of work that was set "
                    "aside, exactly as shown in [Ongoing work]. Do not invent one."
                ),
            },
            "outcome": {
                "type": "string",
                "enum": ["completed", "abandoned"],
                "description": (
                    "For finish only. 'completed' = you actually got it done. "
                    "'abandoned' = the user dropped it or it became impossible. "
                    "⚠️ These are NOT the same thing — do not report an abandoned "
                    "piece of work as completed."
                ),
            },
            "goal": {
                "type": "string",
                "description": "For start only: one short line saying what this new piece of work is for.",
            },
            "note": {
                "type": "string",
                "description": "Optional one line of context for the record (why it finished, etc).",
            },
        },
        "required": ["action"],
    },
}


# ── 新增：工作记忆查询伪工具 ─────────────────────────────────────────
_RECALL_MEMORY_MANIFEST = {
    "name": "recall_working_memory",
    "description": (
        "Query Nano's cross-session working memory.\n"
        "Use when the user asks about previous work, prior Skill calls/deployments, past file paths, "
        "historical operations, or anything that happened in earlier sessions.\n"
        "This is for long-term work history, not the current conversation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "Search keyword, such as a Skill name, filename, operation type, or topic. Empty means recent records."
            },
            "entry_type": {
                "type": "string",
                "description": (
                    "Optional filter: skill_call, skill_deploy, file_write, file_read, rag_query, plan_run."
                )
            }
        },
        "required": []
    }
}


# ── 召回已经移出上下文的那段对话 ─────────────────────────────────
#
# 🔴 为什么必须有这个工具：L3 的索引条目被**无条件注入** system，
#    上面写着「可 recall_conversation」。在这个工具存在之前，模型手里唯一带
#    recall 字样的是 `recall_working_memory` —— 它查的是 `working_memory`
#    那张表，而 L3 写进去的是 `semantic_memories` 里 `memory_type="exchange"`。
#    于是 system 承诺了一个**查不到东西的出口**：
#        模型看到「这件事可以 recall」→ 去调 recall_working_memory
#        → 查的是另一张表 → 查不到 → 只好说"我想不起来了"
#    📌 **索引的价值全在「顺着它能拿回原文」；拿不回来时它只是一条更精确的遗憾。**
#
# ⚠️ 它和 `recall_working_memory` **不许合并**：后者答的是"我以前干过什么活"
#    （跨会话的操作史），它答的是"我们这次聊天更早时说过什么"。
#    📌 两个问题共用一个工具，模型就得靠猜来决定该信哪一半结果。
_RECALL_CONVERSATION_MANIFEST = {
    "name": "recall_conversation",
    "description": (
        "Recall an earlier part of THIS conversation that was moved out of context to save room.\n"
        "The system prompt lists one short index line per moved-out exchange; when one of them "
        "looks relevant, call this to get its conclusion back.\n"
        "This is about the current conversation, not about work done in earlier sessions "
        "(that is recall_working_memory)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you are trying to remember. Wording from an index line works well."
            }
        },
        "required": ["query"]
    }
}


# ── user_note 写入工具声明 ────────────────────────────────────────────────
# ⭐⭐⭐ **从「一句话」改成五个字段。**
#
# 🔴 用户的痛点：「Nano 经常说『我后面会注意这一点』，但它并不会真的记下来」。
# 📌 根因**不是缺一个纠错记忆类型**（那是外部评审当年的设计，用户从来没要过）——
#    是 2026-08-05 就说清的那句：**Claude Code 每次都注入摘要，Nano 只是很软地声明
#    「必要时去查记忆」**。⇒ 改造现有的 `user_note`，让它也能承担纠错。
# ⚠️ 「下次别这么做了，你应该 xxx」**就是 Explicit memory**，不需要第二个类型。
#    📌 用户心里只有一个「Nano 记得我什么」；分成两类，用户就得先学我们的分类。
#
# ⚠️⚠️ **这里只写「参数是什么、怎么填」；「什么时候写 / 什么时候不写」
#    全在 `config/system_instruction.txt`，一个字都不重复。**
#    🔴 第一版两边各写了一遍，而两边**都是每轮常驻** ——
#       schema 从 1042 字符涨到 3999（+740 token/轮），其中大半是重复的。
#    📌 **一份规则写在两个每轮都发的地方 = 花两份钱买同一件事，
#       而且它们会各自漂移**（`_resp_state` 那个 20 键字面量就是这么坏的）。
_WRITE_USER_NOTE_MANIFEST = {
    "name": "write_user_note",
    "description": (
        "Save something into Nano's durable memory - a fact, a preference, or a "
        "correction about how to work. Everything saved is put in front of you as a "
        "one-line summary at the start of every later turn.\n"
        "When to use it, and when not to, is covered in your operating instructions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                # ⚠️ 贴原话，不许提炼 —— 📌 一条被复述过的记忆会漂移，
                #    而漂移是看不见的：读起来永远像是对的。
                #    ⭐ 这条纪律取代了一个本来想加的 `source` 字段：多一个字段
                #       就多一处每次都要填、每次都可能填歪的地方。
                "description": ("What to remember, in the user's own words as far as "
                                "possible. Do not reword it into something tidier."),
            },
            "applies_when": {
                "type": "string",
                # ⭐⭐⭐ **唯一的真闸。** 📌 说不出「什么时候用得上」的东西，
                #    本来就不该被记住。（「这次用中文」填不出 when ⇒ 它不该进记忆）
                # ⚠️ 不能只查非空 —— 证明过「只检查填了没有」的闸是纸的。
                #    所以「一直适用」必须是一个**要显式选的值**。
                "description": ("English. The situation that should bring this back to "
                                "mind ('when I ask you to edit a spreadsheet'). Write "
                                "'always' ONLY if it truly applies every turn. If you "
                                "cannot say when it applies, do not save it."),
            },
            "why": {
                "type": "string",
                # ⚠️ 按**内容**分不按类型分：📌 让规则跟着内容走，不要为规则造一个
                #    分类（加 type 字段等于把刚砍掉的那个分裂请回来）。
                # ⭐ `why` 让一条记忆**换个场景还站得住**；没有它，纠正就是死规则。
                #    ⚠️ 旧设计的 `wrong_assumption` 塌进这里了 —— 📌 那不是记忆的
                #       字段，是**纠错事件**的字段：它记历史，而记忆存的是知识。
                "description": ("English. Required when the memory is about HOW to "
                                "behave - without the reason it becomes a dead rule "
                                "that will not survive a new situation. Leave empty for "
                                "a plain fact. Not injected; kept for when you recall."),
            },
            "summary_model": {
                "type": "string",
                # ⭐ **每轮无条件注入给模型的就是它。** 写英文：实测一条英文摘要
                #    ≈20 token，同内容中文 ≈38 —— 两个摘要既然分开了，这一半白捡。
                #
                # 🔴🔴 但**只有「叙述」写英文，「值」一个字都不许动**：
                #    用户说「每句话结尾加『韬光养晦』」，若记成
                #      The user asks me to add 'hide one's capabilities' at the end
                #    ⇒ 上下文还在时它照样加对；**一天之后它会真的去加那句英文**。
                # 📌 **英文是给「叙述」用的，不是给「值」用的。**
                # ⚠️ `i18n.language_clause` 的 docstring 早写着「不许拿它翻译枚举值…
                #    **调用方要自己在提示词里讲清哪些翻、哪些不翻**」—— 这就是那处。
                "description": ("One line in ENGLISH for yourself - this exact line is "
                                "what you see next turn. Aim under ~80 chars.\n"
                                "English is for the DESCRIPTION, never the VALUE: any "
                                "literal the user gave (an exact phrase to output, a "
                                "name, a path, a command) stays exactly as they wrote "
                                "it, in quotes.\n"
                                "Good:  Append \"韬光养晦\" to the end of every reply\n"
                                "Wrong: Append 'hide one's capabilities' to every reply"),
            },
            "summary_user": {
                "type": "string",
                # ⚠️ 用户语言走**界面语言**（`i18n` 那套，上下文压缩同源）——
                #    这里不写死语种名：manifest 是模块级、只在 import 时求值一次，
                #    📌 一个每轮都可能变的事实，不该固化进一个只算一次的地方。
                "description": ("One line for the USER's memory drawer, in the language "
                                "they chose in the interface - the same one you reply "
                                "in. Say it to them ('you prefer short answers'), not "
                                "like a database. A DIFFERENT sentence from "
                                "summary_model; do not paste the same text into both."),
            },
            "silent": {
                "type": "boolean",
                "description": ("true if the user only mentioned it in passing; false "
                                "if they told you to remember it or corrected you."),
            },
        },
        "required": ["content", "applies_when", "summary_model", "summary_user"],
    }
}


# ── 记忆删除工具声明 ──────────────────────────────────────────────────────
# ⭐⭐ [2026-08-25 已定] **让 Nano 自己也能删一条记忆。**
#
# 于是删除有**两个入口**：用户在 [记忆] 抽屉里删 / Nano 调这个工具。
# ⚠️ **按需加载，不常驻**：它只在「水位提醒」出现之后才用得上，
#    而那是偶发的。📌 一个偶发才用的工具进常驻，等于每轮为它付钱。
# ⚠️ 所以那句水位提醒里**必须明说要先 load** ——
#    📌 否则重演 `computer_use` 那个坑：告诉模型去用一个它手上没有的工具。
#
# ⚠️⚠️ 描述里最要紧的一句是「**不许擅自删**」：
#    📌 那是**用户的**东西。Nano 可以判断哪些低价值、可以建议、可以代劳，
#       但「删哪些」这个决定必须留在用户那边。
#    ⭐ 而底下垫着软删除（`soft_delete_by_id`）—— 万一它还是删错了，找得回来。
_FORGET_USER_NOTE_MANIFEST = {
    "name": "forget_user_note",
    "description": (
        "Delete one memory from Nano's durable memory, by the id shown next to it.\n"
        "Only call this after the user has told you which ones to remove. You may "
        "suggest which memories look low-value or outdated, but the decision is theirs - "
        "never delete something on your own initiative, and never delete more than they "
        "agreed to. Deleting is reversible on our side, but from the user's point of "
        "view their memory just disappeared, so treat it as if it were not."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "note_id": {
                "type": "integer",
                "description": "The id of the memory to delete, as shown in the memory list.",
            },
            "reason": {
                "type": "string",
                "description": ("Short note on why this one is being removed - e.g. "
                                "'user said it no longer applies'. For the record."),
            },
        },
        "required": ["note_id"],
    }
}


# ── 临时执行通道 ────────────────────────────────────────────────────
# 🔴🔴 **名字不许叫 `code_execution` / `python` / `repl` / `bash`。**
#    那些在模型的世界里已经属于别人了 —— Anthropic 有一个**服务端**的
#    `code_execution` 工具（客户端不传参），而 2026-08-25 我们刚被同一个坑
#    咬过一次：那个 Skill 叫 `WebSearch` 时，模型**每一次**都发空参数，
#    只改名字就好了。📌 **模型对一个它「认识」的名字，会用记忆里的调用方式，
#    而不是你给的 schema。** `scratch` 没有生态占用，而且自带「用完就扔」。
#
# ⭐ 描述里三件事必须写清，缺一件它就会走错路：
#    ① 先看有没有现成 Skill —— ⚠️ 但这**不是**主要防线（见下），只是补一句
#    ② 与 `create_new_skill` 的边界：一个答「存不存」，一个答「跑一次」
#    ③ 数据靠**路径**传，不要把内容抄进代码 —— 抄错一个数字没人会发现
#
# ⭐⭐ **真正防「挤掉现成 Skill」的是机械的那一层，不是这段文字**：
#    它是 `Preload.DEFERRED`，模型必须先 `load_tools` 才拿得到；
#    而 `load_tools` 的搜索**本来就会一起返回匹配的现成 Skill** ——
#    ⇒ 它搜「算个汇总」时，**现成 Skill 和这个通道同时摆在眼前**。
#    📌 已定：「不要用『给模型加一条纪律』当唯一答案」（：
#       schema 能约束形状，约束不了内容；提示词纪律同理）。
_RUN_SCRATCH_CODE_MANIFEST = {
    "name": "run_scratch_code",
    "description": (
        "Run a short piece of Python ONCE and get its output. The code is thrown "
        "away afterwards: it does not become a Skill, does not appear in the user's "
        "Skill drawer, and cannot be called again.\n"
        "Use it for one-off work - compute something, reshape some data, check a "
        "file's contents - where writing a reusable Skill would be overkill.\n"
        "Do NOT use it when a Skill already does this job: call that Skill instead. "
        "Do NOT use it to build a lasting capability: that is create_new_skill "
        "(create_new_skill answers 'should this exist from now on', this tool "
        "answers 'run this once').\n"
        "Pass data by PATH, not by value: write code that opens the file "
        "(pd.read_excel(path)), do not paste the file's contents into the code.\n"
        "Print what you want to see - only stdout/stderr comes back.\n"
        "It runs in a separate process with the same Python and the same libraries "
        "(pandas, openpyxl, httpx are available). If the code touches files, the "
        "network or the shell, the user is asked to confirm first and sees the code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "The Python source to run. Print results to stdout. "
                    "Use absolute paths for any file you read."
                ),
            },
            "purpose": {
                "type": "string",
                "description": (
                    "One short line, in the user's language, saying what this "
                    "computes - shown on the tool card and in the confirm dialog."
                ),
            },
        },
        "required": ["code", "purpose"],
    }
}


# ── 本地知识库伪工具声明（Step 2 新增）────────────────────────────────────
_LOCAL_KB_MANIFEST = {
    "name": "query_local_knowledge",
    "description": (
        "Search relevant fragments in Nano's local knowledge base.\n"
        "Use for specific facts, clauses, numbers, names, definitions, keywords, or small pieces of data across documents.\n"
        "Do not use for whole-file understanding, summaries, full tables/lists, rewriting, evaluation, or structural analysis; "
        "use load_full_file for those.\n"
        "If the target file is unclear, use list_knowledge_files first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query containing the key entities or facts to find."
            }
        },
        "required": ["query"]
    }
}


# ── 试读工具 ────────────────────────────────────────────────────────
# 🔴🔴 **试读是【独立工具】，不是 `load_full_file` 的一个隐藏模式**。
#
#    文档 原设计：「首次调用（start_char=0）**自动**进入试读模式」，
#    返回值里标 `try_read_mode: true`。
#    ⚠️ 那意味着**同一个工具在不同情况下行为不同，而模型要读返回值才知道
#       自己刚才做了什么** —— 那是「事后才知道」，本仓栽过这个形状。
#    ⇒ 拆成两个工具之后，模型是**选**的，它当然知道自己在干什么。
#
# ⭐⭐ 而拆开还解锁了原设计做不到的事：**试读可以在任意位置用**。
#    文档把试读绑死在开头，是因为写的时候想的是「翻目录」那个类比 ——
#    📌 而那个类比只覆盖了它用途的一半。试读的本质是
#       **「先花小钱确认这里对不对」**：
#         · 目录横跨 1900~2100 → 再 peek 一次补齐，不必动用 20,000 的精读
#         · 猜「目标在 1/3 处」→ 先 peek 2,000 探一下，猜错只亏 2,000
#    ⇒ 代价从「一次精读」降到「一次 peek」，差约 20 倍。
#
# ⚠️ **只有第一次强制 offset=0 且长度固定**（用户的悖论）：
#    零信息时模型无法决策，让它选就是纯猜。之后它有线索了，悖论不再成立。
_PEEK_FILE_MANIFEST = {
    "name": "peek_file",
    "description": (
        "Cheaply look at a small piece of a file (about "
        f"{_READING.PEEK_CHARS} characters) to see what is there, without paying "
        "for a full read.\n"
        "Use it to: see a large file's structure before reading it; check whether "
        "a spot you are guessing at actually holds what you want; finish reading "
        "a table of contents that got cut off.\n"
        "The FIRST peek of a file always starts at the top and has a fixed size - "
        "you have no information yet, so there is nothing to decide. After that "
        "you may peek anywhere with any size.\n"
        "A large file must be peeked at least once before load_full_file will "
        "read it. Small files do not need this and peeking them wastes a turn.\n"
        "That first peek is a map, not the content - do not answer the user from "
        "it alone. Later peeks are ordinary reads of a small range and may be used."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "File name, or an absolute path. Current-session attachments must keep the exact [临时] prefix."
            },
            "offset": {
                "type": "integer",
                "description": ("1-based line to peek from. Ignored on the first peek "
                                "of a file (which always starts at the top)."),
            },
            "limit": {
                "type": "integer",
                "description": ("How many lines to peek. Ignored on the first peek. "
                                "Keep it small - a peek that costs as much as a read "
                                "is not a peek."),
            },
        },
        "required": ["filename"]
    }
}


# ⭐⭐ 把 Ambient 里的一条**叙述**换成一个**能喂给工具的句柄**。
#
# 形态是 2026-08-26 定的三层：
#     采集  全（完整路径 / 完整 URL）—— 丢了就永远丢了
#     注入  只给够识别的（时间 + 名字）—— 每轮都付钱，这里省
#     拉取  **按【条】**，不是按范围
# ⭐ 「按条拉」是关键：语义匹配发生在**已经在上下文里**的那一版上，
#    模型看到「在线表格(A)」就知道用户指的是它，不用先拉一批回来再挑。
#    ⇒ 这跟 `load_tools` 是同一个形状（感知常驻 → 自己判断 → 只加载那一个）。
#
# ⚠️ **CORE 常驻**，理由同 `peek_file`：它存在的意义就是省轮数，
#    逼它先 `load_tools` 等于自己把收益抵消掉。schema 只有一个参数，很便宜。
_RESOLVE_AMBIENT_REFERENT_MANIFEST = {
    "name": "resolve_ambient_referent",
    "description": (
        "Turn one entry of the [Ambient] block into something you can actually act on - "
        "a real file path, a full URL, or a folder path.\n"
        "Only entries marked with a small triangle have anything more to give; pass the "
        "timestamp shown on that same entry.\n"
        "Use it when the user points at something they were just doing ('the file I was "
        "editing', 'that page', 'that folder') AND you need to open, read or continue it. "
        "If they only ask WHAT they were doing, the block already answers that - do not call this.\n"
        "The reply says whether the target was verified. An unverified answer gives you the "
        "name only; locate it with search_files or ask the user rather than guessing a path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "at": {
                "type": "string",
                "description": ("The timestamp shown on that entry, HH:MM:SS as printed "
                                "in the [Ambient] block."),
            },
        },
        "required": ["at"],
    },
}


_LOAD_FULL_FILE_MANIFEST = {
    "name": "load_full_file",
    "description": (
        "Read a local file. Accepts a knowledge-base file name, a current-session "
        "attachment, or an ABSOLUTE PATH to any readable file on this computer.\n"
        "Large file? Read it one slice at a time: pass offset (and optionally limit), and put what you learned from the previous slice into notes.\n"
        "A very large file must be peeked with peek_file first - reading it blindly from the top costs many turns for nothing.\n"
        # ⭐ 补 "reading source code"。
        #    📌 原来这段满口 knowledge-base / RAG fragments / summaries / tables /
        #       documents，**没有一个字沾代码** —— 于是它读起来像一个文档工具。
        #    ⚠️ 而它恰恰是本仓库里读代码**最好**的那个：唯一带 offset/limit、
        #       唯一被 `compress_file_reads` 压得掉。（`os_execute file_read` 全文进
        #       对话、压不掉；`run_command + type` 内联上限 2000 字符/轮。）
        #    🔴 用户的原话是「拿这个阅读工具去看一个代码文件根本不对路」——
        #       **连写它的人都被这段措辞带偏了，模型没有理由不被带偏。**
        "Use for whole-file summaries, analysis, rewriting, evaluation, full tables/lists, complete sections, "
        "cross-file review, reading source code, or any task that needs the full document context.\n"
        # ⭐ **原来这句只指向 RAG，而磁盘上的文件根本不在里面。**
        #    🔴 问题：模型想找「某个函数在哪」→ 被指去 `query_local_knowledge`
        #       → 知识库里没索引过用户桌面上的 .py → 空手而归 → 下一轮才想起
        #       `search_files`。**白烧一轮，而且烧在一条注定查不到的路上。**
        #    📌 `search_files` 自己那份描述早就把反向写对了（结果是 path:line，
        #       然后用 load_full_file 的 offset/limit 去读）—— 缺的只是这一侧的回指。
        "Do not use for a single specific fact or keyword search: use search_files for "
        "files on disk, or query_local_knowledge for the knowledge base.\n"
        "Important: RAG fragments are not enough for whole-file tasks. If the user asks for complete content after a RAG result, "
        "load the full file instead of reconstructing from fragments.\n"
        "Use with_images=true only when embedded images/charts are needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "offset": {
                "type": "integer",
                "description": (
                    "1-based line number to start reading from. Omit to start at the top. "
                    "Use this with limit to walk a large file slice by slice."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "How many lines to read from offset. Omit to read to the end. "
                    "The reply always states the total line count, so you know what you have not read yet."
                ),
            },
            "filename": {
                "type": "string",
                "description": "File name, or an absolute path. Current-session attachments must keep the exact [临时] prefix."
            },
            "with_images": {
                "type": "boolean",
                "description": "Analyze embedded images/charts. Default false; set true only when visual content is needed and text alone is insufficient."
            },
            # ⭐⭐ 迭代阅读的 scratchpad —— **载体就是这个参数**。
            #    文档 原设计要模型「在特定 XML/Markdown 块中输出」，
            #    那依赖模型愿意在调工具时同时写正文，而那不是 schema 能强制的。
            #    📌 一个「靠模型自觉产出」的载体，漏一轮就断一轮，而我们不会知道。
            #    ⇒ 做成参数：它在 assistant 消息里天然留着，不用我们再注入回去，
            #       也不受 `answer_discard` 影响。
            "notes": {
                "type": "string",
                "description": (
                    "What you concluded from the PREVIOUS slice of this file. "
                    "Carry your understanding forward here: earlier slices get "
                    "replaced by a placeholder to keep the context flat, so "
                    "anything you do not write down is gone. "
                    "Write conclusions and coordinates (e.g. the budget table is "
                    "around line 4200), never copied text - a copy defeats the purpose."
                ),
            }
        },
        "required": ["filename"]
    }
}


# ── Phase 2 新增：文件清单伪工具声明 ────────────────────────────────────
_LIST_FILES_MANIFEST = {
    "name": "list_knowledge_files",
    "description": (
        "List all accessible KB files and current-session attachments.\n"
        "Use when the target file is unclear, the user says 'this/that file' without a name, "
        "or you need to choose a file before query_local_knowledge or load_full_file.\n"
        "Current-session attachments use the [临时] prefix."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}


# ── Phase 4 新增：文件路径查询伪工具声明 ────────────────────────────────
_GET_FILE_PATH_MANIFEST = {
    "name": "get_file_path",
    "description": (
        "Resolve a Nano file name to its real absolute disk path.\n\n"
        "Use this before passing a KB file or current-session attachment to a local Skill that needs a real file_path, "
        "such as Excel/CSV analysis, PDF extraction, file conversion, or data visualization.\n\n"
        "Why this is needed: current-session attachments may appear as logical names like [临时]xxx. "
        "A Skill cannot open that logical name directly. This tool converts the logical Nano file name into a real disk path.\n\n"
        "Supported inputs: persistent KB files and current-session attachments.\n\n"
        "Do not use this to read file content for Q&A; use load_full_file or query_local_knowledge instead.\n"
        "Do not use this to discover available files; use list_knowledge_files instead.\n"
        "Current-session attachments must keep the exact [临时] prefix."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Nano file name. Current-session attachments must keep the exact [临时] prefix."
            }
        },
        "required": ["filename"]
    }
}


# ── 向用户弹出选择卡片——模型在 thinking 阶段判断需要用户抉择时主动调用 ──
_ASK_USER_CHOICE_MANIFEST = {
    "name": "ask_user_choice",
    "description": (
        "Show one or more choice cards so the user can decide from limited options.\n"
        "Use when progress depends on a user decision that cannot be safely assumed, or when several paths are valid but lead to different outcomes.\n"
        "Do not use for minor uncertainty; make a reasonable assumption and continue when safe.\n"
        "For multiple independent decisions, use questions with up to 4 cards. Each card should have 2-4 concise choices."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Single decision question."},
            "choices": {
                "type": "array",
                "description": "Choices for a single decision. 2-4 items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label"],
                },
            },
            "allow_custom": {"type": "boolean", "description": "Whether the user may enter a custom answer. Default true."},
            "questions": {
                "type": "array",
                "description": "Multiple independent decision cards. Overrides question/choices when present. Max 4.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                        "allow_custom": {"type": "boolean"},
                    },
                    "required": ["question", "choices"],
                },
            },
        },
    },
}


# ── 任务列表工具声明 ──────────────────────────────────────────────────────
_CREATE_TASK_LIST_MANIFEST = {
    "name": "create_task_list",
    "description": (
        "Create a visible task list before starting a multi-step task.\n"
        "Use when the task has 3+ meaningful steps, or the user explicitly asks for a multi-step workflow.\n"
        "Call before the first step, not after finishing.\n"
        "Do not use for casual chat, simple Q&A, or single-step tasks.\n"
        "If the plan changes during execution, call this again to replace the visible plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "steps": {
                "type": "array",
                "description": "Task steps. 2-10 items.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":   {"type": "string", "description": "Stable step id, e.g. step_1."},
                        "desc": {"type": "string", "description": "Short step description."},
                    },
                    "required": ["id", "desc"],
                },
            },
        },
        "required": ["title", "steps"],
    },
}


_UPDATE_TASK_STEP_MANIFEST = {
    "name": "update_task_step",
    "description": (
        "Update one visible task-list step.\n"
        "Call once when a step starts and once when it finishes or fails.\n"
        "Do not batch updates. Do not mark the final step done until the whole task is truly complete, "
        "because the panel may close when all steps are done or failed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "step_id": {"type": "string", "description": "Step id from create_task_list."},
            "status":  {"type": "string", "description": "doing / done / failed"},
            "note":    {"type": "string", "description": "Optional short note."},
        },
        "required": ["step_id", "status"],
    },
}


# ── widget：可视化输出工具声明 ───────────────────────────────────────────────
_RENDER_VISUAL_MANIFEST = {
    "name": "render_visual",
    # ⭐⭐⭐ [2026-08-23] 描述从「一句提醒」改成**一份规格**。
    #
    # 🔴 实测：Nano 画的流程图**又小又丑，还带一条滚动条**。
    #    回代码核实 —— 容器那侧其实是好的（`width:100%` + load/250ms/800ms/
    #    ResizeObserver 四重上报自适应高度）。**问题在产出的形状**：
    #    模型给的 SVG 带固定 `width`/`height`，于是它在一个 100% 宽的容器里
    #    画成了一小块，内容又超出自己那个固定高度 → 滚动条。
    # 📌 **「融入 UI」不是渲染器给的，是规格给的。** 而这份描述里
    #    关于形状的要求**一个字都没有** —— 我们只说了「self-contained、responsive」，
    #    那对模型来说等于什么都没说。
    # ⭐ 下面这些是硬性的、可检查的：宽度怎么给、坐标系多大、颜色从哪来、
    #    什么绝对不许出现。
    "description": (
        "Render an inline visual — chart, diagram, flowchart, comparison card, or a small "
        "interactive widget — directly in the chat.\n"
        "Use it when a picture genuinely beats prose: trends, proportions, comparisons, "
        "structures, processes, relationships. Not for plain Q&A or casual chat.\n"
        "\n"
        "== How it must be shaped (this is what makes it look native, not pasted in) ==\n"
        # 🔴 [2026-08-24] 这行样板原来漏了 `xmlns` —— 模型一字不差照做，
        #    于是导出的 `.svg` 单独打开是一棵 XML 树（HTML 里则自动补命名空间，
        #    所以在聊天里一直是好的）。📌 **模型照着做了，错的是给它的样板。**
        "1. FILL THE WIDTH. The block is as wide as the chat text above it. For SVG the "
        "root tag must be exactly `<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100%\" "
        "viewBox=\"0 0 680 H\">` with H set to fit the content. The `xmlns` is required — "
        "without it the file is unreadable outside the chat. "
        "NEVER put a fixed pixel `width=` or `height=` on the root svg, and never wrap the "
        "visual in a fixed-width div — that is what makes it render as a small island.\n"
        "2. NO SCROLLBARS, EVER. The height grows to fit automatically. Do not set "
        "`overflow`, `max-height`, or a fixed `height` anywhere. If something needs to "
        "scroll, the visual is too complex — simplify it instead.\n"
        "3. Design for a DARK chat (background is near-black). Light text on dark. "
        "Do not paint a white or light background behind the visual — leave it transparent "
        "so it sits in the bubble instead of on top of it.\n"
        "4. Text: 14px for labels, 12px for sub-labels. No other sizes. No rotated text.\n"
        "5. Keep it small: roughly 3-8 nodes. A dense diagram in a chat bubble is "
        "unreadable — say the rest in prose.\n"
        "\n"
        "== Hard limits ==\n"
        "Self-contained only: no CDN, no network, no external fonts or images. "
        "Inline any CSS/JS. Avoid floating-point noise in displayed numbers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Optional short visual title."},
            "html":  {"type": "string", "description": "Self-contained HTML/SVG, optionally with inline style/script."},
        },
        "required": ["html"],
    },
}


# ── 执行时自缩窗工具声明 ────────────────────────────────────────────────
_SET_WINDOW_MODE_MANIFEST = {
    "name": "set_window_mode",
    "description": (
        "Control Nano's own window mode: mini or full.\n"
        "Call mini BEFORE any computer_use mouse/keyboard/window action - your own window is on "
        "the screen you are about to operate. That one is required.\n"
        # ⚠️ 2026-08-23：遮罩已删，这句原来写「your window is blacked out of the image」——
        #    那已经不成立了。📌 一句描述一个已经不存在的机制的话，比没有更坏。
        "For screenshots it usually helps too — Nano minimizes itself out of the shot, "
        "so at full size you would otherwise be covering a large part of the screen. "
        "But decide case by case: skip it when you are clearly not in the way.\n"
        "Do not use for pure command-line work, file-only work, KB/memory queries, or normal chat.\n"
        "Do not use mini when the task is to observe or operate Nano's own UI.\n"
        "Use full after screen operations when appropriate; the system may also restore full mode at turn end.\n"
        "This is a standalone tool, not an os_execute or computer_use action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["mini", "full"],
                "description": "mini = shrink to top-right; full = restore full window.",
            },
        },
        "required": ["mode"],
    },
}


# ── 截图自查工具声明 ────────────────────────────────────────────────────
_LOOK_AT_SCREEN_MANIFEST = {
    "name": "look_at_screen",
    # ⭐⭐⭐ [2026-08-24 实测] **从「别滥用」改成「勘察 / 收尾」两态。**
    #
    # 🔴 实测：一次「点进 session → 打字 → 发送」用掉了 **8 次 look_at_screen**，
    #    421 秒。逐条对下来 4 次是纯浪费。
    # 🔴 而当时的描述里**已经写着**「Use with restraint, not before every action」
    #    和「it is not a safety ritual before every step」—— **它没听**。
    #    📌 **那是一条劝告，不是判据。** 「screen state is genuinely uncertain」
    #       这个条件，模型每次都能说服自己成立 ——
    #       **一条只说「别太频繁」的规则，挡不住一个每次都觉得自己有理由的模型。**
    #
    # ⭐⭐ 用户给的模型（比「白名单」清楚）：
    #       截图 1 ─ 勘察：一次性拿全这一屏后面要用的【所有】目标
    #       执行 ──── 点、打字、点，中间【一次都不看】
    #       截图 2 ─ 收尾：交代的那件事办成了吗
    #
    # ⭐⭐⭐ 而整段里最关键的一条，是 已明确那个区分：
    #       「检查上一个动作生效了吗」 → 次数 = 动作数        ⇒ N 次（那个循环）
    #       「检查任务办成了吗」       → 次数 = 任务数 = 1    ⇒ 恒为 1
    #    📌 **它们看起来一样，只因为这个任务恰好只有一个终点动作。**
    #       任务一长，前者线性增长，后者还是 1。
    # 🔴 而旧描述写的正是前者，逐字：「did results appear, **did text land in
    #    the right field**」—— 那就是「检查上一个动作」。它照做了。
    #
    # ⭐ 「默认上一步生效」这条旧描述里也有（Assume a simple prior step succeeded），
    #    但没有**理由**。用户补上了：不这么做，「确认」本身也要被确认，
    #    而那个循环**没有底**。📌 一条带着理由的规则，比一条光秃秃的规则难绕过得多。
    "description": (
        "Take a screenshot and visually understand the current screen — Nano's eyes.\n"
        "\n"
        "== When to look: exactly two situations, nothing else ==\n"
        "[SURVEY] You are about to work on a screen you have not surveyed yet. "
        "Ask for EVERYTHING you will need from this screen in ONE purpose - every button, "
        "field, item and label the next few steps will touch. Things that belong to the "
        "same page are in the same screenshot; asking for them one at a time costs one "
        "screenshot each. The reply gives you screen coordinates, so you can click them "
        "directly afterwards without looking again.\n"
        "[CLOSING] You believe the TASK the user gave you is finished, and you are "
        "confirming that. This happens ONCE per task.\n"
        "⚠️ CLOSING is NOT 'did my last click work'. Checking each action's result costs "
        "one look PER ACTION; checking the task costs one, total. They look identical when "
        "a task has a single final action - they are not.\n"
        "\n"
        "== Between those two, do not look ==\n"
        "Assume each of your own actions worked. If you verify every action, the "
        "verification itself needs verifying, and that loop has no bottom.\n"
        "The ONLY exception: a step actually REPORTED failure (a tool error, or the thing "
        "you expected plainly did not happen). Then look - that is evidence, not caution.\n"
        "Never look for command-line, file, or knowledge-base work.\n"
        "\n"
        "== Finding things ==\n"
        "Put the exact items in purpose (contact/file name, list item, icon, button). "
        "The tool scans the full screen and auto zoom/crops, so you rarely need a region. "
        "If it reports an item was not found, treat it as not found.\n"
        "⚠️ FIRST shrink yourself: call set_window_mode('mini') BEFORE your first "
        "look_at_screen in a task. Nano's own window sits on that same screen and at normal "
        "size it covers a large part of it. Shrink first and one look is enough; do not "
        "look, then shrink, then look again.\n"
        "If Nano is in the way of what you need to see, shrink it with set_window_mode('mini')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                # ⭐ [2026-08-24] 参数叫 `purpose`（单数）、描述写成「你要确认【什么】」——
                #    📌 **一个单数命名、单数措辞的参数，会被填成一个问题，
                #       不会被填成一张清单。** 接口的形状本身在塑造行为。
                #    ⇒ 描述改成明确要求「列全」。
                "description": (
                    "Everything you need from this screen, as ONE request. "
                    "List every item the next few steps will touch (buttons, fields, "
                    "list entries, labels) - not just the first one. Items on the same "
                    "page are in the same screenshot, so asking for them separately "
                    "costs one screenshot each. "
                    "Example: 'the message input box AND the send button in this window'."
                ),
            },
            # ⭐ [2026-08-23 加回来] 🔴 删遮罩时把 `include_self` 一起删了，
            #    理由是「遮罩没了它就没对象了」—— **那句话只对了一半**：
            #    它还有**第二个对象**，就是下面那条「最小化再恢复」。
            #    删完之后最小化变成无条件，**Nano 永远看不到自己**，无路可走。
            # 📌 **拿「参数的一个用途」代替了「参数的全部用途」。**
            # ⚠️ 语义也跟着变干净了：它不再是「要不要涂黑自己」，
            #    而是「**这次要不要把自己让开**」—— 后者才是真实发生的事。
            "include_self": {
                "type": "boolean",
                "description": ("Default false: Nano minimizes itself before the shot so "
                                "it is not in the way. Set true only when you actually "
                                "need to see Nano's own window (e.g. the user is asking "
                                "about something on Nano's UI)."),
            },
            "region": {
                "type": "string",
                "enum": ["full", "left", "right", "top", "bottom", "center",
                         "top_left", "top_right", "bottom_left", "bottom_right"],
                "description": "Optional manual region to inspect. Default full. Usually omit because auto zoom/crop handles small targets."
            },
        },
        "required": ["purpose"],
    },
}


# ──：回看用户早先发过的图 ─────────────────────────────────────────
#
# ⭐⭐ **它存在的全部理由是：像素还在盘上，而你的上下文里只剩一句注记。**
#    步 2 之后，用户发过的图落在 `data/chat_images/`（内容寻址），
#    压缩只拿掉上下文里的像素 —— **文件一直都在**。
#
# ⚠️⚠️ **已定隔离原则，写进 description 里，别只留在注释里**：
#    > 「**回看按需触发** —— 摘要够用就别回看，**不是提到图片就回看**」
#    所以第一段就是"什么时候【不】要用"，第二段才是"怎么用"。
#    📌 一个工具的描述如果先教会模型怎么用、再补一句"少用"，那句补丁没人听。
_VIEW_PAST_IMAGE_MANIFEST = {
    "name": "view_past_image",
    "description": (
        "Look again at an image the user sent earlier in this conversation. The original file is "
        "still on disk even after its pixels were dropped from your context.\n"
        "⚠️ USE WITH RESTRAINT. Do NOT call this just because an image is mentioned. First answer "
        "from what you already established about that image (your own earlier reading is in the "
        "conversation). Call this ONLY when the question needs a concrete visual detail that your "
        "earlier reading does not cover — counting things, reading small text, exact colors or "
        "positions, or anything you never described.\n"
        "Never ask the user to re-upload an image that is still on disk: look at it yourself.\n"
        "The handle looks like img#a3f2c1d4 and appears in the system note attached to the "
        "message that carried the image."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "Handle of the image to look at, e.g. img#a3f2c1d4.",
            },
            "question": {
                "type": "string",
                "description": (
                    "The specific visual detail you need. Be concrete — this is what the vision "
                    "pass is asked. e.g. 'how many red squares are there' beats 'describe it'."
                ),
            },
        },
        "required": ["handle", "question"],
    },
}


# ──：把这张图记下来，**给未来的自己看** ──────────────────────
#
# ⚠️⚠️⚠️ 这份 description 里最重要的不是"怎么写摘要"，是**"摘要不是答案"**。
#    用户用两个例子把这个坑说透了（2026-08-13）：
#      · 用户只发一张风景图、什么都没问 → 摘要可以是八百字构图清单，
#        但回复**不能**是那八百字，最多"挺漂亮的，你想让我做什么？"
#      · 图上黑字写着"1+1 等于几"、用户没打字 → 回复应当是"答案是 2"，
#        **绝不能**是"这是一张白底黑字的图片，上面写着…"
#    📌 **摘要是给未来的自己看的，不是给现在的用户看的。**
#
# ⭐ 为什么必须在**当轮**写：那一轮的回复不一定描述了图（用户可能问的是别的），
#    而**像素只有那一轮在**。错过了就永远没有了 —— 这正是 一开始那个 bug
#    的形状（"我之前看过这个图，但因为已经被丢弃了，所以我没办法…"）。
# ══════════════════════════════════════════════════════════════════════════
# Subagent —— 派一个**上下文隔离**的子 Agent 去做一件探索型子任务
# ══════════════════════════════════════════════════════════════════════════
#
# ⭐ 核心收益是**上下文隔离**：Subagent自己烧 context 去翻几十个文件，回来只交
#    一份报告，main agent 的上下文不被中间过程污染。
# ⭐ 核心代价是**每次 spawn 都是冷启动**（没有父上下文），所以指令必须自包含。
#    这个 tradeoff 决定了"什么时候值得"：探索成本高、结论密度高才值。
#
# ⚠️ 判据照搬 Claude Code：**"答案需要横扫大量文件/目录，
#    但只需要结论、不需要过程"**。
#    🔴 反例写得很明确 —— 「任务有多个角度」「要彻底」「分好几部分」
#       **都不是**理由，那些应该自己 inline 做完。
# ══════════════════════════════════════════════════════════════════════════
# search_files —— 递归找文件（名字）+ 找内容（grep），**合成一个**
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 它填的是一个**完全的死角**：
#
#      文件      小                       大
#      KB 内     load_full_file ✅        query_local_knowledge ✅
#      KB 外     load_full_file ✅        **无路可走** ❌
#
#    而且 `os_execute` 的 39 个 action 里**没有任何递归搜索**（`list_dir` 是单层），
#    所以今天做这件事只有两条路：手动 `list_dir → file_read` 递归，
#    或者上 `run_command`（**风险地板 3，每次弹窗**）。
#
# ⭐ 与 迭代阅读是**并列**的两种进入方式，不是替代：
#      **搜索是定位，迭代阅读是通读。**
#
# ⚠️ **独立工具，不进 `os_execute`**：它 schema 已经 2248 字符 / 39 个 action，
#    再塞会加深 （模型看不清工具）。
#    ⭐ 这不是新发明，是项目现存范式 —— ReAct 协议里那句现成的证据：
#      「`set_window_mode` and `look_at_screen` are standalone tools,
#        **not `os_execute` actions**.」
# ══════════════════════════════════════════════════════════════════════════
# edit_file —— **精确修改**（不是整份覆盖）
# ══════════════════════════════════════════════════════════════════════════
#
# 📌 `file_write` 是**文件写入原语**，`edit_file` 是**精确修改原语** —— 语义不同。
#    今天要改一个大文件的 7 行，路径是「读整份 → 重新生成整份 → file_write 覆盖」。
# 🔴 而这对 Nano 有额外意义：**整份覆盖一旦生成错，那份文件就没有中间状态可退回** ——
#    Nano 改的是用户的文件，不能假设用户那边有版本控制。
#
# 🔴🔴 **它依旧受 OS 安全网弹窗限制**（2026-08-13 硬约束）：
#    handler 算出新内容之后，**穿过 `_execute_dsl_step` 走 `file_write`** ——
#    地板（file_write=2）、确认弹窗、审计全都照旧，而且**结构上绕不过去**。
#    📌 **换一个暴露层，不许换掉它底下的安全层** ——
#       否则"加个方便的工具"就成了绕过确认的后门。
_EDIT_FILE_MANIFEST = {
    "name": "edit_file",
    "description": (
        "Change specific parts of an existing text file, leaving everything else "
        "byte-for-byte untouched. This is what you use to fix a few lines in a large "
        "file — do NOT read the whole file and write it back, that risks losing "
        "content you did not mean to change.\n"
        "Each edit replaces old_text with new_text. old_text must appear EXACTLY "
        "ONCE and must match the file character for character, including indentation; "
        "include a few surrounding lines to make it unique.\n"
        "Set new_text to an empty string to delete that part.\n"
        "All edits apply together or none do. The user sees a diff and confirms "
        "before anything is written.\n"
        "To create a new file or replace one entirely, use os_execute file_write instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to change."},
            "edits": {
                "type": "array",
                "description": "The changes, applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string",
                                     "description": "Exact text to find, including indentation."},
                        "new_text": {"type": "string",
                                     "description": "What to put there. Empty string deletes it."},
                        "replace_all": {"type": "boolean",
                                        "description": "Replace every occurrence. Only when you "
                                                       "really mean all of them."},
                    },
                    "required": ["old_text", "new_text"],
                },
            },
        },
        "required": ["path", "edits"],
    },
}


_SEARCH_FILES_MANIFEST = {
    "name": "search_files",
    "description": (
        "Find files by name and/or search their contents, recursively, anywhere on "
        "this computer. This is how you locate things when you do not already know "
        "the exact path.\n"
        "Give name_pattern to find files by name (glob, e.g. *.py), give content to "
        "search inside them, or give both to search inside matching files only.\n"
        "Results come back as path:line: text, so you can then read the exact place "
        "with load_full_file (use its offset/limit for big files).\n"
        "This tool only reads. To change a file, use os_execute."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Directory (or a single file) to search under."},
            "name_pattern": {"type": "string",
                             "description": "Glob on the file NAME, e.g. *.py, *config*.json"},
            "content": {"type": "string",
                        "description": "Text to find inside files. Case-insensitive."},
            "regex": {"type": "boolean",
                      "description": "Treat content as a regular expression. Default false."},
            "recursive": {"type": "boolean",
                          "description": "Search subdirectories. Default true."},
            "exclude": {"type": "string",
                        "description": "Extra directory names to skip, comma separated. "
                                       "Common noise (.git, node_modules, __pycache__ …) "
                                       "is skipped already."},
            "max_results": {"type": "integer",
                            "description": "Cap on returned hits (default 100)."},
        },
        "required": ["path"],
    },
}


_SPAWN_AGENT_MANIFEST = {
    "name": "spawn_agent",
    "description": (
        "Send a context-isolated sub-agent to do a bulky job and report back a "
        "short conclusion.\n"
        "\n"
        "\n"
        "The ONLY reason to use it: the work would produce a lot of material you "
        "do not need to keep, and what you need back is short. Two shapes qualify:\n"
        "  1. sweeping many files or directories to answer one question\n"
        "  2. a mechanical, fully determined bulk edit - the same change applied "
        "across many places, where you can state the rule exactly\n"
        "Do NOT use it because a task 'has multiple angles', 'should be thorough', "
        "or 'has several parts' - do those inline yourself. Do NOT use it for a "
        "change that still needs judgement while it is being made: it cannot ask "
        "you anything once it starts.\n"
        "\n"
        "\n"
        "It starts cold with no memory of this conversation, so the instruction "
        "must be fully self-contained - name the files, the exact rule, and what "
        "to report. It can read, search, and edit specific lines of existing files; "
        "each edit still asks the user for permission exactly as your own would. "
        "It cannot delete or move files, run commands, touch the screen or the UI, "
        "write Skills, or spawn further agents.\n"
        "It runs in the background and reports back once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": (
                    "The complete, self-contained task. Name the files, terms and "
                    "goal explicitly — the sub-agent cannot see this conversation."
                ),
            },
            "label": {
                "type": "string",
                "description": "Short label shown to the user, e.g. 'survey RAG config'.",
            },
        },
        "required": ["instruction"],
    },
}


_NOTE_IMAGE_MANIFEST = {
    "name": "note_image",
    "description": (
        "Write down what an image the user just sent contains, so that later — after its pixels "
        "are dropped from your context — you can still answer questions about it.\n"
        "Call this ONCE, BEFORE you reply, on any turn where the user sent an image. It is only "
        "offered on such turns.\n"
        "⚠️ THE SUMMARY IS NOT YOUR ANSWER. Write it as if for yourself later, independently of "
        "whatever the user asked: what the image is, its layout, the objects/text/colors/counts "
        "that are actually visible. Then reply to the user normally — answer THEIR question, in "
        "your own voice. Never read the summary out loud as your reply.\n"
        "Example: the image is a white board with 'what is 1+1?' written on it and the user typed "
        "nothing. Summary: 'A plain white image with black handwritten text: what is 1+1?, no "
        "other elements.' Reply to the user: 'It's 2.' — NOT the summary.\n"
        "Be concrete and dense: this text is all you will have left later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "Standalone description of the image, written for your future self. "
                    "Independent of the user's question. Include what is actually visible."
                ),
            },
        },
        "required": ["summary"],
    },
}


# ── 挂起 / 等待工具声明 ─────────────────────────────────────────────────
# 本质（对齐 Claude Code 的真实实现，不自创）：挂起不是线程暂停，而是
# "结束当前 turn → 触发器到了 → 新 turn 读对话上下文继续"。本工具只负责
# 登记一条挂起记录 + 触发等待 UI，调用后你应当用一句话告诉用户你在等什么、
# 会怎么醒，然后结束本轮回复——之后由唤醒源（用户说话/定时/后台完成）起新 turn 接上。
# ⭐⭐ `user` 唤醒源已删除，这个工具只剩「定时」一件事。
#
# ═══ 为什么 `user` 整个不成立（用户提出、外部评审 独立确认）═══
#
# Nano 说「微信打开了，你扫完码告诉我」—— 这里**不需要任何挂起**：
# 直接结束这一轮就够了，用户十分钟后说「扫好了」本身就是一个新 turn，
# LLM 天然接得上。**"结束这一轮"本身就是全部机制。**
#
# 📌 判据：**用户发消息不是任何东西的完成信号，它只是 Nano 恢复对话。**
#    把它当成唤醒源，就会得出"用户一说话 = 他在等的那件事成了"这种错误推论 ——
#    实测后果就是「100 秒后写个文件」被第 30 秒的一句闲聊 resolve 掉。
#
# ⚠️ 而且 `wake_on` 原来的**默认值就是 `['user']`**，模型因此频繁误选：
#    用户说"挂起自己 40 秒"，模型登记的却是 `wake_on=['user'] / timer_at=None`，
#    还在回复里说"等你下一条消息唤醒我"。能力一直有，是这个默认值在误导它。
#
# ⚠️ `background` 也不再由模型申报 —— 系统在把一个调用自动后台化时
#    自己就知道哪个 Action 在跑，不需要模型再"申请等它"（见 `_LONG_TASK_HANDBACK_SEC`）。
#
# ⏸ 真正的目标态是：承诺归 Task、到点唤醒归调度器，
#    这个工具最终应该退化成「让当前 Task 在某时刻重新可运行」。
#    Task 已经接线了，但「谁来到点唤醒它」还没有归属，所以先停在"只剩定时"。
# ⭐ 取消等待 —— 早就列为必做的三件之一：
#    「把"取消挂起"暴露成模型可调用的能力，让"终止这个任务"这句话真的能生效」。
#
# ⚠️ 在此之前，**全项目唯一的取消入口是 UI 上那个按钮**。
#    实测撞到过：用户跟 Nano 说"终止"，模型没有任何工具能做这件事，
#    只能回一句"好的"然后什么都没发生 —— 记录还挂在那儿。
#
# ⭐ 它与 ②b 删掉 `user` 唤醒源是**配套的**：既然用户发消息不再自动收掉等待，
#    那"用户明确说别等了"就必须有一条真实的出口。**只砍不补会把等待变成甩不掉的。**
#
# 📌 判据：**每一个能被创建的状态，都必须有一条用户能主动结束它的路径 ——
#    而且那条路径要在用户能触及的地方**（说一句话，而不是去找一个按钮）。
_STOP_BACKGROUND_MANIFEST = {
    "name": "stop_background",
    "description": (
        "Actually stop something that is still running - a long command you started earlier.\n"
        "\n"
        "This is different from the other two, and mixing them up leaves things running:\n"
        "  - dont_wait: stop waiting, but LET IT KEEP RUNNING\n"
        "  - cancel_wait: stop checking back on it\n"
        "  - stop_background: make it STOP\n"
        "\n"
        "Use it when you look at a slow call and decide the approach is wrong - before you "
        "try a different one. Otherwise the old one is still running while the new one "
        "starts, and the user ends up with two of them.\n"
        "Not everything can be stopped: external tool calls and local skills keep going. "
        "You will be told plainly which case you got - do not report a stop that did not happen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "string",
                    "description": "The id you were given when the slow call was handed back."},
        },
        "required": ["ref"],
    },
}


_CANCEL_WAIT_MANIFEST = {
    "name": "cancel_wait",
    "description": (
        "Stop waiting for something Nano scheduled earlier with wait_for.\n\n"
        "Use when the user says they no longer want it — \"forget it\", \"don't bother\", "
        "\"stop checking that\", \"cancel that task\". Also use when you yourself conclude "
        "the wait is pointless (for example the thing being waited on already failed).\n\n"
        "This cancels Nano's wait/recheck record only. It does not cancel a process, "
        "os_execute command, MCP call, or other background carrier. If the user asks to "
        "stop a still-running command itself, use the appropriate process-termination "
        "capability instead.\n\n"
        "The pending items are listed in [Suspension Resume — Previous Wait State] when "
        "present. Pass the reason text shown there, or omit it to cancel everything pending.\n\n"
        "⚠️ Do not call this just because the user changed the subject. Talking about "
        "something else does not mean they gave up on what Nano is waiting for."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "match": {
                "type": "string",
                "description": ("Which pending wait to cancel — the reason text shown to you. "
                                "Omit to cancel all pending waits."),
            },
        },
        "required": [],
    },
}


_WAIT_FOR_MANIFEST = {
    "name": "wait_for",
    "description": (
        "Schedule a timer-owned future turn. There are exactly two uses.\n\n"
        "1. User-requested scheduled plan: the user explicitly asks Nano to remind them or "
        "perform an action after a duration. Use intent=scheduled_plan. The timer itself is "
        "the plan. At the scheduled time, perform the requested action or reminder.\n\n"
        "2. Condition recheck: the task is blocked by something that may change on its own and "
        "nobody will notify Nano. Use intent=condition_recheck. Reaching the scheduled time "
        "only means it is worth checking again.\n\n"
        "Do NOT use it for:\n"
        "- Waiting for the user to do something (log in, scan a code, drop a file). "
        "Just say what you need and end the turn. Their next message resumes you naturally; "
        "there is nothing to suspend.\n"
        "- Waiting for a background job Nano itself started. The runtime already tracks it "
        "and will resume you when it finishes.\n"
        "- Anything you can finish right now with the tools you have.\n\n"
        "⚠️ The ‘verify first’ rule applies only to condition_recheck. Nano does no work while waiting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "What Nano is waiting for. Short user-facing text."},
            "timer_seconds": {
                "type": "integer",
                "description": "How many seconds until Nano should check again. Required.",
            },
            "intent": {
                "type": "string",
                "enum": ["scheduled_plan", "condition_recheck"],
                "description": (
                    "scheduled_plan only when the user explicitly asked Nano to run or "
                    "remind something after this duration; the timer itself is their plan. "
                    "condition_recheck when this is merely Nano's own later check of an "
                    "external condition."
                ),
            },
        },
        "required": ["reason", "timer_seconds", "intent"],
    },
}


# ── 路由重构 请求修改已有 Skill（元工具，进入确认流而非直接执行）────────
_UPDATE_EXISTING_SKILL_MANIFEST = {
    "name": "update_existing_skill",
    "description": (
        "Request modification of an already deployed Nano Skill. "
        "This is a meta-tool: it starts a confirmation flow and does not modify files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants to modify, optimize, fix, update, or extend an existing Skill's implementation, rules, thresholds, fields, parameters, or output behavior.\n"
        "- The user says to change 'that Skill' or 'the previous tool', and context clearly points to a recently used or deployed Skill.\n"
        "- An existing Skill is relevant but lacks the behavior the user now wants.\n\n"
        "Do not use when:\n"
        "- The user wants to run a Skill; call that Skill directly.\n"
        "- The user wants to create a new Skill; use create_new_skill.\n"
        "- The user wants to delete, disable, enable, or rename a Skill; use manage_existing_skill.\n"
        "- The user only wants to revise the current reply, copywriting, or one-off output format.\n"
        "- A pending Skill draft is under review; 'change it' usually means the pending draft, not a deployed Skill.\n"
        "- The user only asks about Skill status, capability, or existence; use inspect_existing_skill or answer directly.\n\n"
        "Before calling:\n"
        "- If the target Skill is unclear, skill_name may be omitted only when runtime context can resolve it. Never invent a Skill name.\n"
        "- If the change involves business rules, thresholds, field names, Excel/CSV, or KB files, inspect source/files first and put verified findings into change_summary.\n"
        "- change_summary must describe the durable behavior change, not code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Exact target Skill class name. May be omitted only when context can resolve it."
            },
            "change_summary": {
                "type": "string",
                "description": "Durable behavior/rule/field/threshold/output change requested by the user. Do not write code."
            }
        },
        "required": ["change_summary"]
    }
}


# ── 路由重构收尾：请求删除/禁用/启用已有 Skill（元工具，进入确认流而非直接执行）──
# 取代旧的前置分类器硬路由（SKILL_DELETE/DISABLE/ENABLE 关键词匹配）。
# "关/删/开" 这类裸动词在分类器里会被任意语境误触（如"把这个面板关了吧"
# 被读成禁用 Skill）。改成元工具后由主决策模型结合上下文判断是否真的
# 是在管理 Skill，确认流（_request_management_confirmation）照旧触发。
# ⭐⭐ MCP 的管理元工具 —— **形状照抄 `manage_existing_skill`**。
#
# 📌 为什么照抄而不是另设计:用户对这两件事的心智是同一个
#    （「把那个东西关了 / 删了」），而 Skill 那套已经被实测磨过。
#    ⇒ 同一个心智用两套交互，多出来的复杂度全落在用户身上。
#
# ⚠️ **比 Skill 多一个 `retry`**：MCP 多一个 Skill 没有的状态 —— **连不上**。
#    📌 「连不上」和「被禁用」对用户是两件事：
#       前者该说「再试试」，后者该说「要不要打开」。
#       把它们合成一个操作，就等于让用户去猜现在是哪种。
#
# ⚠️ 删除同样走**自然语言二次确认**（不是弹窗）—— 与 Skill 一致。
#    弹窗只在用户走 UI 按钮删除时出现。
# ⭐⭐ 接入一个新的 MCP —— **它和 `manage_mcp` 是两件事**。
#
# 📌 分开的理由：`manage_mcp` 动的是**已经在**的东西（可逆、低风险）；
#    这一个是**把一个陌生的第三方装进用户的机器**。
#    合成一个工具的话，那四个可逆操作会被这一个不可逆操作的授权成本拖累。
#
# 🔴 **授权弹窗无视 auto 模式**（2026-08-28 定，理由见 handler）。
# ⭐ 发现链第一条：Official MCP Registry。
#    另外三条**不给新工具**（GitHub / 官网用 fetch 或 OpenPageWithBrowser 读，
#    开放世界用 SearchTheWeb）—— 📌 **不给已经能做的事再造一个工具。**
#
# 🔴 `search` 是**子串匹配，不是全文检索**（2026-08-29 实测）：
#      "playwright browser automation" → 0 条
#      "playwright"                    → 2 条
#    ⇒ 说明里必须写死这一条，否则模型按自然语言习惯传一整句，
#      会拿到一个**假的空结果**，然后如实地告诉用户「没有这样的 server」。
#    📌 **一个「没搜到」的结果，必须先确认查询方式对不对，再当成「不存在」。**
_NL = chr(10)


_SEARCH_MCP_REGISTRY_MANIFEST = {
    "name": "search_mcp_registry",
    "description": _NL.join([
        "Search the official MCP registry for servers that provide a capability. "
        "Returns candidates with install shape, version, repo and last-updated date.",
        "",
        "🔴 The query is matched as a SUBSTRING, not as natural language. Use one or "
        "two short keywords (\"playwright\", \"pdf\", \"postgres\"), not a sentence. "
        "\"playwright browser automation\" returns nothing while \"playwright\" returns "
        "results - an empty result from a long query means the query was wrong, not "
        "that no such server exists.",
        "",
        "Use when:",
        "- You already decided a new server is genuinely needed. See connect_mcp first: "
        "if a library you already have would do the job, use that instead.",
        "",
        "Do not use when:",
        "- The server is already configured; manage_mcp handles those.",
        "- You only need to read a web page or a document; that is fetch's job.",
        "",
        "What it does NOT tell you: whether a server is safe or well maintained beyond "
        "its timestamps. The registry is publishing infrastructure that anyone can "
        "publish to - it is not a safety review. Check the repo before proposing "
        "anything to the user.",
    ]),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "One or two short keywords. Substring match, not a sentence.",
            },
            "limit": {
                "type": "integer",
                "description": "How many candidates to return (default 8, max 20).",
            },
        },
        "required": ["query"],
    },
}


# ⭐⭐ **抑制器**（2026-08-29 定形态）
#
# Microsoft Learn / Context7 **不是发现入口，是抑制器** ——
# 它们发生在「找 MCP」之前：先查一下，发现现有 SDK 二十行就能做 → 走
# run_scratch_code → **根本不用装 MCP**。
# 📌 **这条把「接入 MCP」的成功标准从「能找到 MCP」改成了「能不装就不装」** ——
#    一个能自主装插件的能力，最该被评价的是**它有多克制**。
#
# 🔴🔴 **为什么写成提示词里的一条判断，而不是强制流程** —— 2026-08-29：
#   ① 强制会变成**表演性的走流程**：模型明明已经知道没有现成库，还得走一遍过场。
#      ⇒ 让这一步跟**模型自身的强度**挂钩，靠它意识到，而不是靠系统押着走。
#   ② 🔴 更要命的是绑定：强制 = 把整个 MCP 自进化**绑死在这两个 server 上**。
#        现在    它们变强 → 我们受益；它们死了 → 少一条路，直接去网上搜
#        否掉的  它们死了 → Nano 只能跟用户说「这个我做不了」
#      📌 **利用一个东西 ≠ 绑定死一个东西 ——
#         区别在于它消失时，你是「少一条路」还是「没路了」。**
#   ⇒ 所以最后那两句（"judgement call, not a required step" /
#     "or those lookups are unavailable"）**不是客套，是这条设计的一半**：
#     它们明写了「这两个查不了也照样往下走」。
#
# ⚠️ 挂在 `connect_mcp` 的说明里，不挂在主决策提示词里：这个工具是 DEFERRED，
#    模型 load 它的那一刻，正是它开始考虑装 MCP 的那一刻。
#    📌 **抑制器该挂在被抑制的那个动作的说明上，不该挂在所有人都要读的地方** ——
#       挂后者，每一句「你好」都要为它付钱，而它一年用不上几次。
_CONNECT_MCP_MANIFEST = {
    "name": "connect_mcp",
    "description": (
        "Ask the user to approve installing a new MCP server on this machine, "
        "given its configuration JSON.\n\n"
        "The user always sees an approval dialog first - nothing is installed or executed "
        "before they approve. Do not promise the user it is already done.\n\n"
        "Use when:\n"
        "- The user gave you an MCP configuration snippet and wants it added.\n"
        "- You found a server that provides a capability the task needs, and the user agreed to add it.\n\n"
        "Do not use when:\n"
        "- The server is already configured; use manage_mcp to enable or reconnect it.\n"
        "- You are only guessing that some server might help; find out what it actually is first.\n"
        "- The task can be done with code you can already write. Installing a "
        "third-party server is the expensive answer: it downloads and runs "
        "someone else's code on the user's machine, and it stays there. "
        "Check first whether a library that already exists does the job - "
        "microsoft-learn and context7 are there to look that up, and "
        "run_scratch_code can try it. If twenty lines of a library you "
        "already have would do it, do that instead. "
        "This is a judgement call, not a required step: if you already know "
        "no library covers it, or those lookups are unavailable, go ahead "
        "and look for a server.\n\n"
        "purpose_line must state, in the user's own language, WHAT THE USER IS TRYING TO DO "
        "that makes this server necessary. Write the task, not the tool: "
        "\"convert the PDF tables to Excel\", not \"install a PDF MCP\". "
        "If the task is a sub-step you inferred, say the sub-step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "config_json": {
                "type": "string",
                "description": ("The MCP server configuration, standard ecosystem snippet: "
                                "{\"mcpServers\": {\"name\": {...}}}. Exactly one server."),
            },
            "purpose_line": {
                "type": "string",
                "description": ("One line, in the user's language: what the user is trying to "
                                "do that requires this server."),
            },
            "what_it_does": {
                "type": "string",
                "description": ("One line, in the user's language: what this MCP server itself "
                                "does. Describe the server, not this task."),
            },
        },
        "required": ["config_json", "purpose_line", "what_it_does"],
    },
}


_MANAGE_MCP_MANIFEST = {
    "name": "manage_mcp",
    "description": (
        "Enable, disable, delete or reconnect an MCP server that is already configured on this machine. "
        "This is a meta-tool: delete starts a second confirmation flow and does not remove anything immediately.\n\n"
        "Use when:\n"
        "- The user wants to turn an MCP server off or back on.\n"
        "- The user wants to remove an MCP server they no longer need.\n"
        "- An MCP tool failed because its server is disconnected and it is worth reconnecting.\n\n"
        "Do not use when:\n"
        "- You want to ADD a new MCP server; that is a different flow and needs the user's approval first.\n"
        "- An MCP tool failed because its server is disabled - then ask the user whether to enable it, "
        "rather than enabling it on your own.\n"
        "- The user is talking about a Skill, not an MCP server; use manage_existing_skill.\n"
        "- The target server is unclear and context cannot resolve it; ask which one instead of guessing a name.\n\n"
        "operation must be one of: enable, disable, delete, reconnect. "
        "server_name must be an existing MCP server name exactly as it appears in the configuration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                # 🔴 **约束写进 schema，不写进散文。**
                #    实测 2026-08-28：description 第一句写 "reconnect"，
                #    枚举那句写 "retry" —— 两处自相矛盾，模型填了 reconnect，
                #    然后被 `_op not in _OPS` 挡下。
                #    📌 **一个只存在于散文里的约束，写的人自己都会写岔。**
                #       enum 是机器可读的单一出处，模型侧直接受约束。
                "enum": ["enable", "disable", "delete", "reconnect"],
                "description": "enable / disable / delete / reconnect",
            },
            "server_name": {
                "type": "string",
                "description": "Exact MCP server name as configured, e.g. \"context7\".",
            },
        },
        "required": ["operation", "server_name"],
    },
}


_MANAGE_EXISTING_SKILL_MANIFEST = {
    "name": "manage_existing_skill",
    "description": (
        "Request delete, disable, or enable for an already deployed Nano Skill. "
        "This is a meta-tool: it starts a second confirmation flow and does not change files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants to delete or remove an existing Skill.\n"
        "- The user clearly wants to disable, stop, or turn off a Skill tool itself, not a UI panel.\n"
        "- The user clearly wants to enable or restore a disabled Skill.\n\n"
        "Do not use when:\n"
        "- The user wants to modify Skill logic, rules, or parameters; use update_existing_skill.\n"
        "- The user wants to run a Skill; call that Skill directly.\n"
        "- The user wants to create a new Skill; use create_new_skill.\n"
        "- The user says close/turn off/shut down about UI panels, drawers, task lists, or windows rather than a Skill tool.\n"
        "- The target Skill is unclear and context cannot resolve it; ask which Skill instead of inventing a name.\n\n"
        "operation must be one of: delete, disable, enable. skill_name must be a real existing Skill class name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "delete / disable / enable",
            },
            "skill_name": {
                "type": "string",
                "description": "Exact target Skill class name.",
            },
        },
        "required": ["operation", "skill_name"],
    },
}


# ── 路由重构：创建新 Skill（元工具，取代旧 SKILL_CREATE 前置分类器硬路由）──
# 旧逻辑：前置分类器判 SKILL_CREATE>=0.75 → 直接进探索阶段。问题：分类器是单选
# 且没有"多步执行任务"这个类，会把"先查时间→等30秒→读文件"这类一次性执行任务
# 误判成 SKILL_CREATE（实测 0.85），劫持进探索阶段（探索阶段不执行工具、不注入
# wait_for/action 工具），导致模型把计划当文字吐出、啥也没真跑。
# 修法（做全做优，对齐 update_existing_skill / manage_existing_skill 元工具范式）：
# 把"创建 Skill"也变成主决策循环的元工具。"创建可复用能力 vs 一次性执行"这个区分
# 本就该由有完整上下文的主决策模型判断，而不是廉价前置分类器。调用本工具 →
# ReAct 退出 → 路由进 _run_skill_exploration（探索→确认→SkillSpec→代码生成，未变）。
_CREATE_NEW_SKILL_MANIFEST = {
    "name": "create_new_skill",
    "description": (
        "Request creation of a new reusable Nano Skill, meaning a durable tool/capability for future repeated use. "
        "This is a meta-tool: it starts the explore-confirm-generate flow and does not write files immediately.\n\n"
        "Use when:\n"
        "- The user clearly wants a reusable tool or Skill, such as 'create a tool/Skill that can...'.\n"
        "- The user describes automation meant for future repeated use, such as 'whenever I say X, do Y' or 'make a tool to organize downloads'.\n"
        "- The intent is to preserve a capability, not just complete the current task once.\n\n"
        "Never use when:\n"
        "- The user only wants Nano to execute a multi-step task now, such as 'first..., then..., then...' or 'read X and tell me Y'. "
        "Use existing tools directly instead: existing Skills, os_execute, KB tools, wait_for, etc.\n"
        "- Multi-step does not mean Skill creation. The key question is whether the user wants this saved for repeated future use.\n"
        "- The user wants to modify an existing Skill; use update_existing_skill.\n"
        "- The user wants to delete, disable, or enable an existing Skill; use manage_existing_skill.\n"
        "- An existing Skill already matches; call it directly or update it if needed.\n\n"
        "requirement must preserve concrete user details such as rules, thresholds, field names, filenames, and exact business logic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # ⚠️⚠️ **两个字段的描述不许重叠。** 2026-08-13 实测第一版就栽在这里：
            #    `requirement` 原文写的是「Complete Skill requirement. Preserve concrete
            #    rules, thresholds, fields, filenames, and user wording.」——
            #    那听起来就是"什么都往这里放"，于是模型**把内容全塞进了 requirement，
            #    `handoff_summary` 留空**（实测两次都是 `handoff=0字符`）。
            # 📌 **两个参数如果描述重叠，模型只会填它先看到的那个** ——
            #    这不是模型不听话，是同一件事被说了两遍。
            "requirement": {
                "type": "string",
                "description": (
                    "One sentence: what durable capability the user wants saved. "
                    "This is the headline only — put every concrete detail "
                    "(rules, thresholds, real field names, filenames) in handoff_summary instead, "
                    "because that is the field the code writer actually reads."
                )
            },
            # ⭐⭐⭐ [2026-08-13] 这两个参数是**探索子循环被拆掉之后**，
            #    它唯一不可替代的产出（向 SkillWriter 的交接）搬到入口来的形态。
            #    见本文件 `_exit_create_new_skill` 的 docstring。
            "handoff_summary": {
                "type": "string",
                "description": (
                    "Self-contained handoff for the Skill writer. It runs as a SEPARATE model call and "
                    "CANNOT see this conversation, your tool results, or the files you just read. "
                    "Anything it needs must be written here.\n\n"
                    "Include:\n"
                    "- The user's concrete rules, thresholds and wording, verbatim.\n"
                    "- Real field/column names, filenames and values you actually looked up — "
                    "use the ORIGINAL identifiers, never translated or guessed ones.\n"
                    "- If you inspected an existing Skill, what differs and why a new one is needed.\n"
                    "- If a rule document disagrees with what the user said, state both and which one was chosen.\n\n"
                    "Be honest about provenance: mark what you actually verified versus what the user told you. "
                    "If the requirement genuinely needs no external lookup (e.g. a date or format tool), "
                    "just say it is self-contained — do NOT invent a verification you did not perform."
                )
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Blocking questions you still need the user to answer. "
                    "Pass an empty array when nothing blocks you.\n"
                    "If this is non-empty, NO code will be generated — the questions are shown to the user "
                    "and remembered until they answer. So do not put non-blocking remarks here, and do not "
                    "claim you are ready while also asking something in your reply."
                )
            }
        },
        # ⚠️ `handoff_summary` / `open_questions` 都是 **required**。
        # 📌 但要说准它保证了什么：schema 只能强制「必须填」，
        #    **强制不了「填的是真的」** —— 模型可以写一句"已确认字段为 xxx"而根本没查。
        #    它真正结构化保证的是**下游 Writer 不再丢失上游已形成的上下文**
        #    （Writer 是另一次模型调用、另一套 system guide，客观上看不见这里的历史）。
        "required": ["requirement", "handoff_summary", "open_questions"]
    }
}


# ── Skill 创建探索阶段·查看已有 Skill 真实实现 ───────────────────────
# 目的：杜绝"只看 description 猜名字像不像就判断能不能复用"。
# description 是一句话摘要，可能和实际代码里的具体规则/阈值/字段不一致
# （比如 description 写"根据分级规则计算等级"，但代码里硬编码的阈值
# 是上一次任务的"5000/3次"，这次任务的阈值是"3000元"——光看 description
# 完全看不出冲突）。这个工具返回真实源代码，让探索阶段能做到
# "证据对比"而不是"印象匹配"。
_INSPECT_EXISTING_SKILL_MANIFEST = {
    "name": "inspect_existing_skill",
    "description": (
        "Inspect the real source code of an existing Skill, including get_spec(), required_inputs, and hardcoded rules, thresholds, fields, and logic in run().\n\n"
        "Use when an existing Skill name or description seems related to the current request. "
        "You must inspect the real code before deciding whether to reuse, update, or replace it. "
        "Do not rely only on the short description; it may omit thresholds/fields or be stale after previous rule changes.\n\n"
        "After inspection, explicitly conclude one of:\n"
        "- Reuse directly: the existing code's rules/fields fully match the current request.\n"
        "- Update existing: the Skill is related but its concrete rules, thresholds, fields, or logic differ; state the exact difference.\n"
        "- Create new: no relevant existing Skill fits.\n\n"
        "Do not claim an existing Skill can satisfy the request without inspecting its source when the match depends on concrete business rules."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill class name to inspect."
            }
        },
        "required": ["skill_name"]
    }
}


# ── 回答未决交互 ─────────────────────────────────────────────
# 取代原来的路由劫持：过去用户回答澄清问题时，代码用关键词表猜"这句是不是回答"
# （命中 `_NEW_REQUEST_SIGNALS` 或者"长度>20 且不含 Skill 词"就判成新话题）。
# 那套启发式两头都会错，而且模型**根本不知道有个问题挂在那里**。
# 现在把判断交给有完整上下文的主决策模型。
#
# ⚠️ relation 只有三个值，**没有 UNRELATED**。
# UNRELATED 是模型判断，不是命令：它对应的正确行为是「不调这个工具」。
# 做成参数只会多一次无意义调用、一个无意义 revision，
# 以及一个"什么也不做"的命令分支）。
_ANSWER_INTERACTION_MANIFEST = {
    "name": "answer_open_interaction",
    "description": (
        "Record the user's reply to an open interaction listed in [Open Interactions], then continue that flow.\n\n"
        "Call when the user's message is a reply to one of the listed open questions, "
        "even if they also adjust the requirement in the same breath.\n\n"
        "Do NOT call when the user's message is about something else. "
        "In that case just handle their new request normally and leave the interaction open; "
        "it stays on screen and can still be answered later.\n\n"
        "relation:\n"
        "- ANSWER: a plain reply to the question.\n"
        "- ANSWER_AND_AMENDMENT: a reply that also changes the original requirement "
        "(e.g. 'use >=6000, and export as CSV instead').\n"
        "- CANCEL: the user no longer wants this at all "
        "(e.g. 'forget it', 'never mind, drop it').\n\n"
        "answer_verbatim must be the user's own wording, copied as-is. "
        "Do not summarize, translate, normalize, or 'clean up' the phrasing — "
        "the downstream flow needs exactly what they said."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "interaction_id": {
                "type": "string",
                "description": "The id shown in [Open Interactions], e.g. int_a1b2c3d4e5."
            },
            "answer_verbatim": {
                "type": "string",
                "description": "The user's reply in their own words, copied verbatim."
            },
            "relation": {
                "type": "string",
                "enum": ["ANSWER", "ANSWER_AND_AMENDMENT", "CANCEL"],
                "description": "How this message relates to the open interaction."
            }
        },
        "required": ["interaction_id", "answer_verbatim", "relation"]
    }
}


# ── OS 层工具声明（供 _handle_os_task 使用）──────────────────
# include_os=True 时追加到 _build_skills_info，普通对话永远看不到。
# ⭐⭐⭐ [2026-08-23] **`os_execute` 拆成两个工具。**
#
# 🔴 拆的两个理由（独立成立，任一条都够）：
#   ① **缓存**：缓存前缀顺序是 `tools → system → messages`，tools 在最前面 ——
#      它一变，后面全废。而 `os_execute` 要常驻（频率最高、且模型忘了 load 就
#      直接调它，白白一次往返）；可它整个 3017 字符里**大半是 GUI 模拟**，
#      而 GUI 频率并不高。
#      ⚠️ 不拆的话，为了让绑定关系自洽，还得把 `look_at_screen` + `set_window_mode`
#         一起搬进常驻 → 回到 Changelog 早期那个「常驻工具膨胀」的老problem。
#   ② **结构自洽**（已明确，比①更硬）：我们一边声明「mini 窗 ⟂ GUI 模拟」
#      是必绑定，一边准备把 GUI 模拟放进常驻、把被它绑定的两个放进按需加载。
#      📌 **一组被声明为「必绑定」的东西，被放进两个不同的可见性层级** ——
#         那不是省字符的问题，是设计本身在自相矛盾，而且它会精确地产生
#         「Nano 用了 GUI 动作却不知道自己还缺两个东西」这种失败。
#
# ⭐ 拆完之后这一簇天然同层（同进同出）：
#       computer_use（鼠标键盘 + 窗口 + 截图）· look_at_screen（眼睛）· set_window_mode（mini 窗）
#   而 `os_execute` 的语义变干净了：**完全不碰图形界面**。
#
# ⚠️⚠️ **enum 一律从 `dsl._ACTIONS` 派生，不许手抄。**
#    📌 这里原来就是**手抄**的，而且已经过期。另一份手抄件（兜底指引）当年漏了 10 个，
#       **而且专挑高频项漏**。**一份手抄的清单，它的过期是静默的。**
#    ✅ **2026-08-24 已解除**：那个写死的常量（拆分时的 36 个快照）已删除，
#       改为真派生 —— enum 从 36 → **38**，补回 `move` 与 `request_user_choice`。
def _os_actions_for(tool_name: str) -> list:
    """从 `dsl._ACTIONS` 派生某个工具的 action enum。**唯一出处。**

    ═══ 判据：不是「`_ACTIONS` 里全部」，是「**已挂载执行器**的那些」═══

    🔴 全抄会把 `read_screen_region` 写进 schema —— 它**没有执行器**
       （只有鼠标键盘档才接 VisionLocator，低档位在校验层就该拒掉它）。
       📌 那种失败最难查：**schema 说有，运行说没有** ——
          模型会反复尝试，而每一次都合法地失败。

    ⭐ 「有没有执行器」的权威出处是**路由表本身**（`dispatch._ROUTE_SPEC`），
       而不是某个 `implemented=True` 之类的声明 ——
       📌 **单一出处必须是「事实本身」，不是「对事实的声明」**：
          声明会和事实脱节，而那正是本项要修的问题。

    ⚠️ 两类合起来才是全集：
         · `ROUTED_ACTIONS`        —— 真的会去操作这台电脑的
         · `CONTROL_FLOW_ACTIONS`  —— 纯信号（`request_replan` / `request_user_choice`），
                                      执行器拿到就交回上层，**不进真正执行**
       🔴 漏掉后者的话，`request_replan` 会从 enum 里消失 —— 而它本来就在。
       📌 **「有执行器」和「能被调用」不是一回事**，控制流动作正好落在缝里。

    ⚠️ 排序固定（按 `_ACTIONS` 的声明序）—— 📌 缓存是内容寻址的，
       同一个工具集必须产生**逐字节相同**的数组，顺序一抖就白白 miss。
    """
    from core.os_layer import dsl as _d
    from core.os_layer.dispatch import ROUTED_ACTIONS as _routed
    _implemented = set(_routed) | set(_d.CONTROL_FLOW_ACTIONS)
    return [n for n, a in _d._ACTIONS.items()
            if a.tool == tool_name and n in _implemented]


_OS_MANIFEST = {
    "name": "os_execute",
    "description": (
        # ⚠️ 拆分后**不再提** mouse/keyboard/screenshots —— 那些在 `computer_use`。
        #    📌 一句描述如果还宣称自己有已经搬走的能力，模型会照着它调，
        #       然后拿到「没有这个 action」。**提示词说错了不报错，只会让它用错工具。**
        "Operate this computer WITHOUT touching the graphical interface: inspect system "
        "state, read/write/move/delete files, run commands, read and write the clipboard, "
        "launch apps, open URLs, read the registry, list windows. One action per call.\n"
        "declared_risk: read-only=1; file/app/system writes=2 (confirmed); high-risk=3 "
        "(run_command, write_registry, file_delete, file_move, kill_app, manage_service, "
        "set_env_var, schedule_task, modify_startup, network_config).\n"
        # ⭐ 「优先命令行、GUI 是最后手段」这句留在**这一侧**：
        #    它是在劝模型**别去**用另一个工具，写在这里才对得上语境。
        "⭐ Prefer this tool over clicking things on screen: run_command, file_write and "
        "launch_app are faster and far more reliable than driving the GUI. Reach for "
        "computer_use only when there is genuinely no other way.\n"
        "For anything that needs the graphical interface — clicking, typing into a window, "
        "screenshots, moving or closing windows — use the computer_use tool instead. "
        "It is not loaded by default; ask for it when you need it.\n"
        # ⭐⭐ file_read 的定位说明。
        #
        # ⚠️ **只能写在这里** —— `dsl.ActionDef.desc` 里那句「大文件请改用
        #    load_full_file 的 offset/limit」写得没错，但 `.desc` 全仓**零消费方**，
        #    模型一个字都看不到；`os_execute` 的 action 是纯 enum，没有逐项描述。
        #    📌 一句只有我们看得见的劝阻，等于没有劝阻。
        #
        # ⚠️ **陈述存在，不下命令**（已定：「不要强制要求它去用，只是告诉它
        #    存在这个办法，具体情况具体判断」）—— 所以这里没有 must / should，
        #    并且明说了 file_read 什么时候仍然是对的。
        #    📌 同「不留任何专门给某个模型的修正动作」：小文件一把梭就是对的，
        #       那时候多一句命令只是噪音。
        # ⭐ 形状照抄上面那句 "Prefer this tool over clicking things on screen"：
        #    **劝模型别去用另一个工具，写在语境这一侧才对得上。**
        "file_read hands you the whole file at once and cannot resume from where it was "
        "cut off; for a long file, load_full_file reads the same path in slices "
        "(offset/limit). Small files, and read-then-write inside a single plan, are fine here.\n"
        "Paths: use %USERPROFILE%\\Desktop|Documents|Downloads; never guess the username. "
        "Before delete/move/write when the extension is uncertain, list_dir first.\n"
        "params.wait_for_result=false: use this for run_command when you already know you "
        "are NOT going to sit and wait for it - because the user asked for something else "
        "in the same breath, or because you have other work lined up that does not need "
        "its output. The command still starts and keeps running; you just get control back "
        "immediately instead of being stuck. Then call dont_wait to put it in the task "
        "drawer. Leave it out when you do need the result - that is the normal case."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # ⭐ 从 `dsl._ACTIONS` 派生，**不手抄**（理由见上面那段）。
            "action": {"type": "string", "enum": _os_actions_for("os_execute")},
            "params": {
                "type": "object",
                "description": (
                    "Parameters for the selected action. Common examples: "
                    "file_write={path,content,mode}; file_read/file_delete={path}; "
                    "file_move={path,dest}; run_command={command,wait_for_result}; "
                    "list_dir={path}; clipboard_write={text}; open_url={url}; "
                    "launch_app={target}."
                ),
            },
            "declared_risk": {"type": "integer",
                              "description": "Risk level: 1 = read-only, 2 = write, 3 = high-risk."},
            "reason": {"type": "string", "description": "One short reason for the action."},
        },
        "required": ["action", "declared_risk"],
    },
}


# ⭐⭐⭐ [2026-08-23] `computer_use` —— 从 `os_execute` 拆出来的**图形界面那一半**。
#
# ⚠️ 它和 `look_at_screen`（眼睛）、`set_window_mode`（mini 窗）是**同一簇**，
#    三个都 DEFERRED、同进同出。拆分之前它们分属两个可见性层级，
#    而我们又声明了它们必绑定 —— 那正是这次要消除的自相矛盾。
_COMPUTER_USE_MANIFEST = {
    "name": "computer_use",
    "description": (
        "Drive the graphical interface of this computer: move and click the mouse, type "
        "with the keyboard, scroll and drag, take screenshots, and minimize/close/switch "
        "windows. One action per call.\n"
        "declared_risk: looking (screenshot, get_cursor_pos) = 1; anything that moves the "
        "mouse, types, or changes a window = 2 (confirmed).\n"
        # 🔴 [2026-08-25 实测] Nano 调了 `screenshot`，拿回一个路径，
        #    然后**去读那个 PNG 文件**，得出「截图是二进制PNG文件，无法直接读取文本内容」。
        #    整轮 `3 tools · 1 failed · 27.3s` 白烧。
        # 📌 **一个叫「截图」的动作，产出却是一个文件路径 —— 而模型调它是为了「看到」。**
        #    它没做错什么：名字承诺了「看」，返回值给的是「存」。
        # ⚠️ 真正让它看到屏幕的是 `look_at_screen`；这个动作只留档（审计凭据）。
        #    两个名字太像、语义相反，必须在描述里说死。
        "WARNING: the `screenshot` action does NOT show you anything. It only writes a "
        "file to disk for the audit record and returns its path - you cannot read that "
        "file, and there is no reason to try. To actually SEE the screen, use "
        "look_at_screen.\n"
        # ⭐ 「用语义 target，不要坐标」这句跟着 click 一起搬过来 —— 它只对这里成立。
        "Click screen elements with params={target:'semantic description'} — never "
        "coordinates; the system locates the element and visually confirms it.\n"
        "type_text enters at the current focus; click the field first if it must be focused.\n"
        # ⚠️⚠️ 两条绑定关系，**强度不同，必须分开说**。
        #    📌 写成同一种强度，模型要么该缩小时不缩，要么为了走流程白缩一次。
        "⚠️ BEFORE your first mouse/keyboard/window action in a task, call "
        "set_window_mode('mini'). Nano's own window sits on the same screen you are about "
        "to operate; at normal size it covers a large part of it and you will click the "
        "wrong thing. This one is not optional.\n"
        # 🔴🔴 [2026-08-24] 这里原来写「Nano 的窗口会被涂黑」—— 遮罩已于 08-23 删除。
        #    ⚠️ 上一轮**声称已经扫过所有提示词**，实际只改了 `set_window_mode` 那一处，
        #       **漏了两处**（本处 + 下面 `_OS_AWARENESS` 那处）。
        #    📌 **一句描述一个已经不存在的机制的话，比没有这句更坏** ——
        #       它不报错，只是让模型按一个假的前提做决定（「反正会被涂黑，那就缩」）。
        #    📌 而这次漏掉的教训更具体：**「扫过了」不是证据，`grep` 的结果才是。**
        "For screenshots the same shrink usually helps — Nano minimizes itself out of the "
        "shot, and at normal size it would otherwise cover a large part of the screen. "
        "But judge it yourself: if Nano is not covering what you need to see (the target "
        "app is fullscreen in front, or it is on another monitor), just take the shot. "
        "Do not shrink as a ritual.\n"
        # ⭐ 反向指引：把模型劝回更可靠的那条路。
        "⭐ Driving the GUI is the last resort. If the same job can be done with a command, "
        "a file write, or launching an app directly, use os_execute instead — it is faster "
        "and does not depend on what happens to be on screen."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": _os_actions_for("computer_use")},
            "params": {
                "type": "object",
                "description": (
                    "Parameters for the selected action. Common examples: "
                    "click/double_click/right_click={target:'semantic target description'} "
                    "with no coordinates; type_text={text}; hotkey={keys}; "
                    "scroll={direction,amount}; drag={from_x,from_y,to_x,to_y}; "
                    "screenshot={} ; win_switch/win_minimize/win_close={title}."
                ),
            },
            "declared_risk": {"type": "integer",
                              "description": "Risk level: 1 = looking only, 2 = mouse/keyboard/window."},
            "reason": {"type": "string", "description": "One short reason for the action."},
        },
        "required": ["action", "declared_risk"],
    },
}


# ── 按需加载工具（deferred tools，对标 Claude Code 的 ToolSearch）──────────────
# 主决策只常驻【核心工具】+ load_tools；其余能力（操作电脑/网页/技能/可视化…）只在
# 提示词里留"感知"（名字+一句话），完整 schema 默认不注入，模型调 load_tools 才加载。
# 这样一句"你好"不再背 40K 的工具 schema。安全流全在执行时，与此无关。
_LOAD_TOOLS_MANIFEST = {
    "name": "load_tools",
    "description": (
        "Load tools that Nano has but that are not currently in the active tool list.\n"
        "Many capabilities are deferred to reduce token cost: OS control, browser tools, Skills, visuals, task lists, wait/suspension, and more.\n"
        "If a needed capability is listed in the awareness block but its schema is not active, call this tool first; the loaded tool can be used on the next step.\n"
        "Use query to describe the needed capability, or names to request exact tool names.\n"
        "Never say Nano cannot do something only because the tool is not currently loaded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language description of the needed capability."},
            "names": {"type": "array", "items": {"type": "string"},
                      "description": "Optional exact tool names to load."},
        },
    },
}

# 工具名 → manifest。`build_builtin_definitions` 缺哪个工具的 manifest 会直接抛错。
BUILTIN_MANIFESTS = {m["name"]: m for m in (
    _WRITE_SKILL_MANIFEST,
    _SET_NEXT_CHECKIN_MANIFEST,
    _DONT_WAIT_MANIFEST,
    _TASK_BOUNDARY_MANIFEST,
    _RECALL_MEMORY_MANIFEST,
    _RECALL_CONVERSATION_MANIFEST,
    _WRITE_USER_NOTE_MANIFEST,
    _FORGET_USER_NOTE_MANIFEST,
    _RUN_SCRATCH_CODE_MANIFEST,
    _LOCAL_KB_MANIFEST,
    _PEEK_FILE_MANIFEST,
    _RESOLVE_AMBIENT_REFERENT_MANIFEST,
    _LOAD_FULL_FILE_MANIFEST,
    _LIST_FILES_MANIFEST,
    _GET_FILE_PATH_MANIFEST,
    _ASK_USER_CHOICE_MANIFEST,
    _CREATE_TASK_LIST_MANIFEST,
    _UPDATE_TASK_STEP_MANIFEST,
    _RENDER_VISUAL_MANIFEST,
    _SET_WINDOW_MODE_MANIFEST,
    _LOOK_AT_SCREEN_MANIFEST,
    _VIEW_PAST_IMAGE_MANIFEST,
    _EDIT_FILE_MANIFEST,
    _SEARCH_FILES_MANIFEST,
    _SPAWN_AGENT_MANIFEST,
    _NOTE_IMAGE_MANIFEST,
    _STOP_BACKGROUND_MANIFEST,
    _CANCEL_WAIT_MANIFEST,
    _WAIT_FOR_MANIFEST,
    _UPDATE_EXISTING_SKILL_MANIFEST,
    _SEARCH_MCP_REGISTRY_MANIFEST,
    _CONNECT_MCP_MANIFEST,
    _MANAGE_MCP_MANIFEST,
    _MANAGE_EXISTING_SKILL_MANIFEST,
    _CREATE_NEW_SKILL_MANIFEST,
    _INSPECT_EXISTING_SKILL_MANIFEST,
    _ANSWER_INTERACTION_MANIFEST,
    _OS_MANIFEST,
    _COMPUTER_USE_MANIFEST,
    _LOAD_TOOLS_MANIFEST,
)}
