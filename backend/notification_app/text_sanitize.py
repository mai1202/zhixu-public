"""显示文本清理：微信占位符、私用区字符和控制字符不应出现在标题与摘要里。

来源原文必须逐字保留以支持证据回链，因此只清理「展示用」标题、摘要和建议；
写入时清理一次保证新数据干净，读取时再清理一次让升级前已有的脏数据也能正常显示。
"""

from __future__ import annotations

import re
from typing import Any

# 微信把附件、链接、表情等渲染成方括号文本，导出后这些标签只剩噪声。
_PLACEHOLDER_TOKENS = (
    "链接",
    "文件",
    "图片",
    "视频",
    "语音",
    "动画表情",
    "表情",
    "位置",
    "名片",
    "小程序",
    "聊天记录",
    "红包",
    "转账",
    "直播",
    "音乐",
    "公众号",
    "视频号",
    "接龙",
    "卡券",
    "礼物",
    "文件传输助手",
)

# 微信表情在纯文本导出里是「[玫瑰]」这类名字；标题里逐字保留只会变成一串括号。
_EMOJI_NAMES = (
    "微笑 撇嘴 色 发呆 得意 流泪 害羞 闭嘴 睡 大哭 尴尬 发怒 调皮 呲牙 惊讶 难过 酷 "
    "冷汗 抓狂 吐 偷笑 可爱 白眼 傲慢 饥饿 困 惊恐 流汗 憨笑 大兵 奋斗 咒骂 疑问 嘘 "
    "晕 折磨 衰 骷髅 敲打 再见 擦汗 抠鼻 鼓掌 糗大了 坏笑 哈欠 鄙视 委屈 快哭了 阴险 "
    "亲亲 吓 可怜 菜刀 西瓜 啤酒 篮球 乒乓 咖啡 饭 猪头 玫瑰 凋谢 示爱 爱心 心碎 蛋糕 "
    "闪电 炸弹 刀 足球 瓢虫 便便 月亮 太阳 礼物 拥抱 强 弱 握手 胜利 抱拳 勾引 拳头 "
    "差劲 爱你 爱情 飞吻 跳跳 发抖 怄火 转圈 磕头 回头 跳绳 挥手 激动 街舞 献吻 "
    "左太极 右太极 福 烟花 爆竹 庆祝 合十 加油 汗 破涕为笑 捂脸 机智 耶 奸笑 无语 "
    "嘿哈 皱眉 吃瓜 OK NO"
).split()

_TOKENS = sorted(set(_PLACEHOLDER_TOKENS) | set(_EMOJI_NAMES), key=len, reverse=True)
_BRACKET_TOKEN = re.compile(
    r"[\[【]\s*(?:" + "|".join(re.escape(token) for token in _TOKENS) + r")\s*[\]】]"
)
# 私用区字符是微信自定义字体图标（在浏览器里必然显示成方块），零宽与双向控制不可见且会破坏排版。
_INVISIBLE = re.compile(
    "["
    "\u0000-\u0008\u000b\u000c\u000e-\u001f"
    "\u007f-\u009f"
    "\u200b\u200c\u200e\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u2069"
    "\ufeff\ufffd"
    "\ue000-\uf8ff"
    "\U000f0000-\U000ffffd\U00100000-\U0010fffd"
    "]"
)
# 数字键帽（1⃣️）缺少字体时只剩「1」和方块，保留数字即可。
_KEYCAP = re.compile(r"\ufe0f?\u20e3")
_SURROGATE = re.compile("[\ud800-\udfff]")
_SPACES = re.compile("[ \t\u00a0\u3000]+")
_EDGE = re.compile(r"^[\s\-–—·:：,，、|/\\]+|[\s\-–—·:：,，、|]+$")


def clean_display_text(value: Any, *, limit: int = 500, fallback: str = "") -> str:
    """清理标题/摘要里的占位符与不可见字符；清理后为空时返回 fallback。"""
    text = "" if value is None else str(value)
    # 未配对代理项无法编码成 UTF-8，先丢弃，避免下游 JSON 或渲染出现方块。
    text = _SURROGATE.sub("", text)
    text = _BRACKET_TOKEN.sub(" ", text)
    text = _INVISIBLE.sub("", text)
    text = _KEYCAP.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _EDGE.sub("", text)
    text = text.strip()
    return text[:limit] if text else fallback


def clean_title(value: Any, fallback: str = "未命名通知") -> str:
    """标题比摘要更短，清理后为空时用一个明确占位而不是原样返回噪声。"""
    return clean_display_text(value, limit=500, fallback=fallback)
