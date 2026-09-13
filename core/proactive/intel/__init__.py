# core/proactive/intel/
"""
Nano 主动智能 + 情感系统（v0）。

本目录的设计已经固定，实现以代码为准。
本子包是新系统，与旧的 triggers.py / speaker.py 并存，待就绪后整体切换。

铁律（架构不变量，见 types 注释）：
  情感只流向 ①主动开口闸门 ②注入提示的语气提示；
  绝不进入工具/OS 执行的准入逻辑。executor 永远拿不到 mood。
"""
