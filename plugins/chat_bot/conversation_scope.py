"""One-way conversation visibility: private can read groups, never the reverse."""
from collections import deque


def session_key(event, user_id=None):
    uid = str(user_id if user_id is not None else event.user_id)
    gid = getattr(event, 'group_id', None)
    return f'group:{gid}' if gid is not None else f'private:{uid}'


def persisted_key(key):
    key = str(key)
    if key.startswith(('private:', 'group:')):
        return key
    # Legacy history mixed both sources. Never expose it to a group.
    return 'private:' + key


HEADER = "【同一用户的群聊上下文】"
ROLE_SEPARATOR = "："

_MAX_GROUPS = 4
_MAX_MESSAGES_PER_GROUP = 8
_MAX_BODY_CHARS = 1800
_MAX_TOTAL_CHARS = 5000


def sender_name(sender) -> str:
    if isinstance(sender, dict):
        name = sender.get("card") or sender.get("nickname") or ""
    else:
        name = getattr(sender, "card", "") or getattr(sender, "nickname", "") or ""
    return " ".join(str(name).replace("【", "").replace("】", "").split())[:48]


def speaker_label(user_id: str, name: str = "") -> str:
    return f"{name}（QQ {user_id}）" if name else f"QQ {user_id}"


def attributed_message(message: dict, user_id: str = "") -> dict:
    uid = str(message.get("speaker_id") or user_id)
    role = message["role"]
    content = str(message["content"])
    if uid:
        label = speaker_label(uid, sender_name({"card": message.get("speaker_name")}))
        heading = "发言人" if role == "user" else "回复给"
        content = f"【{heading} {label}】\n{content}"
    return {"role": role, "content": content}


def _take_last_messages(rendered, limit=_MAX_MESSAGES_PER_GROUP):
    """Return up to the final ``limit`` items from ``rendered``."""
    if limit <= 0:
        return []
    if isinstance(rendered, (list, tuple)):
        return rendered[-limit:]
    return list(deque(rendered, maxlen=limit))


def _tail_body(messages, limit=_MAX_BODY_CHARS):
    """Build at most the final ``limit`` Unicode characters of formatted lines."""
    if not messages or limit <= 0:
        return ""
    chunks = []
    remaining = limit
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        pieces = (
            str(msg.get("content", "")),
            ROLE_SEPARATOR,
            str(msg.get("role", "user")),
        )
        for piece in pieces:
            if not piece:
                continue
            if len(piece) >= remaining:
                chunks.append(piece[-remaining:])
                remaining = 0
                break
            chunks.append(piece)
            remaining -= len(piece)
        if remaining == 0:
            break
        if idx > 0:
            chunks.append("\n")
            remaining -= 1
            if remaining == 0:
                break
    chunks.reverse()
    return "".join(chunks)


def group_context(histories, user_id, render):
    """Format the trailing context window for up to four matching user groups."""
    selected = []
    for key in reversed(histories):
        if not isinstance(key, str) or not key.startswith("group:"):
            continue
        if key.count(":") == 1:
            session = histories[key]
            own = [m for m in session.recent if str(m.get("speaker_id")) == str(user_id)]
            if own:
                selected.append((session, own[-_MAX_MESSAGES_PER_GROUP:]))
        elif key.endswith(f":{user_id}"):
            selected.append((histories[key], None))
        if len(selected) == _MAX_GROUPS:
            break

    blocks = []
    for session, own in reversed(selected):
        messages = [attributed_message(m) for m in own] if own is not None else render(session)
        body = _tail_body(_take_last_messages(messages))
        if body:
            blocks.append(f"{HEADER}\n{body}")
    return "\n\n".join(blocks)[-_MAX_TOTAL_CHARS:]


def group_history_tail(session, render_all):
    """Return the last eight recent items, falling back to ``render_all`` when short."""
    recent = session.recent
    if len(recent) >= _MAX_MESSAGES_PER_GROUP:
        return recent[-_MAX_MESSAGES_PER_GROUP:]
    return render_all(session)[-_MAX_MESSAGES_PER_GROUP:]


def quote_allowed(event, data):
    gid = getattr(event, 'group_id', None)
    if gid is None:
        return True
    return (data.get('message_type') == 'group'
            and str(data.get('group_id', '')) == str(gid))
