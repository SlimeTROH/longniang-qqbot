"""聊天插件和 OneBot 私聊权限共用的指令语法。"""
from __future__ import annotations

AFF_QUERY_WORDS = frozenset({"好感度", "好感度查询", "查询好感度", "好感"})
AFF_HISTORY_WORDS = AFF_QUERY_WORDS | {"查好感", "affection"}
AFF_HISTORY_SUBS = frozenset({"历史", "记录", "回忆", "history"})
AFF_RESET_SUBS = frozenset({"重置", "清零", "归零"})


def split_aff_query(text: str) -> tuple[str, str]:
    body = text.strip().removeprefix("/").strip().casefold()
    if not body:
        return "", ""
    words = body.split(maxsplit=1)
    if len(words) == 2:
        return words[0], words[1]
    for names, subs in ((AFF_HISTORY_WORDS, AFF_HISTORY_SUBS), (AFF_QUERY_WORDS, AFF_RESET_SUBS)):
        for sub in subs:
            if body.endswith(sub) and body[:-len(sub)] in names:
                return body[:-len(sub)], sub
    return body, ""


def is_affection_history(text: str) -> bool:
    head, sub = split_aff_query(text)
    return head in AFF_HISTORY_WORDS and sub in AFF_HISTORY_SUBS


def is_image_quota_query(text: str) -> bool:
    return text.strip().removeprefix("/").strip() == "图像配额"
