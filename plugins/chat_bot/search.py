"""联网搜索（DeepSeek 自研 Search API）

用途：若娜瓦的知识库不全，遇到她不确定的事实类问题时，
先搜一下再把资料给她，避免她靠猜来编。

接口：POST https://api.deepseek.com/v1/tools/search
    同一个 API Key，按次计费（2026-11-01 前免费，之后 5 元/千次）。
"""

from __future__ import annotations

import asyncio
import datetime
import re
import time
from dataclasses import dataclass, field

import httpx

from nonebot import logger

SEARCH_URL = "https://api.deepseek.com/v1/tools/search"

# 带这些词的提问，多半在问"当前/最新"，搜索时应该限定时间范围，
# 否则搜索引擎会把几个月前的旧文章当成"下个版本"给你。
RECENCY_WORDS = (
    "最新", "下个", "下期", "下版本", "下一版", "下个版本", "下一个版本", "下一版本",
    "现在", "目前", "当前", "这期", "这一期", "这版本", "这版本",
    "新版本", "下一个", "最近", "近期", "今天", "今年", "这周", "本月",
    "什么时候出", "什么时候上", "几号出", "上线了吗", "出了吗",
)


def wants_recency(text: str) -> bool:
    """这句话是不是在问"当前/最新"的情况。"""
    t = (text or "").strip()
    return any(word in t for word in RECENCY_WORDS)


# 「图里这是谁 / 这是什么 / 哪部作品的」—— 这类问题必须查
IMAGE_QUESTION_WORDS = (
    "是谁", "什么人", "哪个角色", "哪位", "什么作品", "哪部", "出自",
    "什么东西", "是什么", "叫啥", "叫什么",
)


def looks_like_image_question(text: str) -> bool:
    """他是不是在问图里的东西是什么。"""
    t = (text or "").strip()
    if not t:
        return False
    has_ref = any(w in t for w in ("图", "这张", "这图", "截图", "画面"))
    has_ask = any(w in t for w in IMAGE_QUESTION_WORDS)
    return has_ref and has_ask


def extract_quoted_hints(desc: str, limit: int = 3) -> str:
    """从图片描述里抓「引号中的文字」当搜索线索。

    视觉模型认不出角色是谁，但通常能读出画面上的横幅、标题、水印。
    「国模憋气大赛」这种词往往比人物长相更能搜到出处。
    """
    if not desc:
        return ""
    hits = re.findall(r"[「『“\"]([^」』”\"]{2,20})[」』”\"]", desc)
    cleaned: list[str] = []
    for h in hits:
        h = h.strip()
        if h and h not in cleaned:
            cleaned.append(h)
    return " ".join(cleaned[:limit])


# ======================================================================
# 强制搜索意图
#
# 用户明确说「查一下 / 搜搜 / 上网看看」时，不该再让模型去猜"她本来知不知道"，
# 直接搜。误搜一次只花 0.005 元，漏搜一次就是胡编。
# ======================================================================
_FORCE_WORDS = (
    # 查
    "查一查", "查查", "查一下", "查下", "去查", "帮我查", "帮我查查",
    "帮我查一下", "你查查", "你查一下", "查查看",
    # 搜
    "搜一搜", "搜搜", "搜一下", "搜下", "去搜", "帮我搜", "帮我搜搜",
    "帮我搜一下", "你搜搜", "你搜一下", "搜搜看",
    # 上网
    "上网查", "上网看", "网上查", "网上搜", "联网查", "联网搜", "上网搜",
    # 核实
    "确认一下", "帮我确认", "核实一下", "帮我核实", "验证一下",
    # 找
    "帮我找找", "去查查看",
)


def detect_force_search(text: str) -> bool:
    """检测用户是否明确要求去查/搜（这类请求一律强制联网）。"""
    if not text:
        return False
    t = text.strip()
    return any(word in t for word in _FORCE_WORDS)


def clean_query(text: str) -> str:
    """把「帮我查一下 XXX」这类指令词剥掉，只留查询主体。

    剥得太多（剩下的太短）就退回原句——搜索引擎本来就是按自然语言工作的，
    留点噪声反而比留下一个孤零零的「看」要好。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    t = raw
    for word in ("帮我", "给我", "麻烦", "请", "你", "快", "先", "去"):
        if t.startswith(word):
            t = t[len(word):].lstrip("，,、:： ")
    for word in _FORCE_WORDS:
        if word in t:
            t = t.replace(word, " ")
    # 顺手清掉残留的方向词
    for word in ("上网", "网上", "联网"):
        t = t.replace(word, " ")
    t = " ".join(t.split()).strip("，,、:： 。．!！?？")

    # 剥掉太多（或本来就没内容）就用原句
    if len(t) < max(4, len(raw) * 0.4):
        return raw[:128]
    return t[:128]


@dataclass
class SearchHit:
    """一条搜索结果。"""

    title: str = ""
    url: str = ""
    summary: str = ""       # 长摘要（600~800 字符），给模型读
    snippet: str = ""       # 短摘要
    date: str = ""
    authority: int = -1


@dataclass
class SearchResult:
    """一次搜索的结果。"""

    ok: bool = False
    query: str = ""
    hits: list[SearchHit] = field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.hits


# ======================================================================
# 引擎 1：百度 AI 搜索
#
# 实测 2026-10-19：0.9~1.5 秒返回 20 条，带发布日期和内容摘要，
# 中文内容（尤其原神/国内游戏）召回明显强于国外引擎。
# 免费额度 100 次/天。
# ======================================================================
BAIDU_URL = "https://qianfan.baidubce.com/v2/ai_search"


async def _search_baidu(
    query: str, key: str, count: int, timeout: float, recency: bool
) -> SearchResult:
    started = time.time()
    body: dict = {"messages": [{"role": "user", "content": query[:72]}]}
    # 百度支持时间过滤：week / month / semiyear / year
    if recency:
        body["resource_type_filter"] = [
            {"type": "web", "top_k": max(count, 10), "search_recency_filter": "month"}
        ]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(BAIDU_URL, headers=headers, json=body)

    if resp.status_code != 200:
        return SearchResult(
            query=query,
            error=f"百度 HTTP {resp.status_code}: {resp.text[:160]}",
            elapsed=time.time() - started,
        )

    data = resp.json()
    refs = data.get("references") or []
    hits: list[SearchHit] = []
    for item in refs[: max(count * 3, 10)]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                summary=content[:800],
                snippet=content[:200],
                date=str(item.get("date") or "").strip(),
            )
        )
    return SearchResult(
        ok=True, query=query, hits=hits, elapsed=time.time() - started
    )


# ======================================================================
# 引擎 2：Tavily（只在判定需要国外/英文资料时才走这条）
# ======================================================================
TAVILY_URL = "https://api.tavily.com/search"


async def _search_tavily(
    query: str, key: str, count: int, timeout: float
) -> SearchResult:
    started = time.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            TAVILY_URL,
            headers={"Content-Type": "application/json"},
            json={
                "api_key": key,
                "query": query,
                "max_results": max(1, min(10, count)),
                "search_depth": "basic",
                "include_answer": True,
            },
        )

    if resp.status_code != 200:
        return SearchResult(
            query=query,
            error=f"Tavily HTTP {resp.status_code}: {resp.text[:160]}",
            elapsed=time.time() - started,
        )

    data = resp.json()
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                summary=content[:800],
                snippet=content[:200],
                date=str(item.get("published_date") or "").strip(),
            )
        )
    # Tavily 的 AI 摘要插到最前面，模型读这一段就够
    answer = str(data.get("answer") or "").strip()
    if answer:
        hits.insert(
            0,
            SearchHit(
                title="Tavily AI 摘要",
                url="",
                summary=answer[:800],
                snippet=answer[:200],
            ),
        )
    return SearchResult(
        ok=True, query=query, hits=hits, elapsed=time.time() - started
    )


# ======================================================================
# 引擎 3：ddgs（免费兜底，需要走代理）
# ======================================================================
async def _search_ddgs(
    query: str, count: int, timeout: float, proxy: str
) -> SearchResult:
    started = time.time()

    def _run() -> list[dict]:
        from ddgs import DDGS

        # 依次试 bing → yandex → auto。
        # 实测 2026-10-19：DuckDuckGo 默认后端对中文新内容收录很差
        # （「若娜瓦」搜不到），换成 bing 立刻就有 4 条权威结果。
        last: Exception | None = None
        for backend in ("bing", "yandex", "auto"):
            try:
                kwargs: dict = {"timeout": int(timeout)}
                if proxy:
                    kwargs["proxy"] = proxy
                with DDGS(**kwargs) as d:
                    rows = list(d.text(query, max_results=max(1, min(10, count)), backend=backend))
                if rows:
                    logger.info(f"search: ddgs 用 {backend} 后端拿到 {len(rows)} 条")
                    return rows
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
        if last is not None:
            raise last
        return []

    try:
        rows = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        return SearchResult(
            query=query,
            error=f"ddgs {type(exc).__name__}: {str(exc)[:120]}",
            elapsed=time.time() - started,
        )

    hits = [
        SearchHit(
            title=str(r.get("title") or "").strip(),
            url=str(r.get("href") or r.get("url") or "").strip(),
            summary=str(r.get("body") or "").strip()[:800],
            snippet=str(r.get("body") or "").strip()[:200],
            date=str(r.get("date") or "").strip(),
        )
        for r in rows
        if isinstance(r, dict)
    ]
    return SearchResult(ok=True, query=query, hits=hits, elapsed=time.time() - started)


# ======================================================================
# 引擎 4：DeepSeek 搜索（最后一层兜底）
# ======================================================================
async def _search_deepseek(
    query: str,
    api_key: str,
    count: int,
    timeout: float,
    ori_question: str,
    recency: bool,
) -> SearchResult:
    started = time.time()

    if recency and "after:" not in query:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=75)).strftime("%Y-%m-%d")
        query = f"{query} after:{cutoff}"

    payload: dict = {
        "query": query[:128],
        "count": max(1, min(50, count)),
        "need_content": False,
    }
    if ori_question:
        payload["ori_question"] = ori_question[:128]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(SEARCH_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        return SearchResult(
            query=query,
            error=f"DeepSeek HTTP {resp.status_code}: {resp.text[:160]}",
            elapsed=time.time() - started,
        )

    data = resp.json()
    hits: list[SearchHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                summary=str(item.get("summary") or "").strip(),
                snippet=str(item.get("snippet") or "").strip(),
                date=str(item.get("date") or "").strip(),
                authority=int(item.get("authority") or -1),
            )
        )
    return SearchResult(ok=True, query=query, hits=hits, elapsed=time.time() - started)


# ======================================================================
# 对外入口
#
#   需要国外资料 → Tavily（外网分支）
#   否则         → 百度 → ddgs → DeepSeek（国内链）
#
#   任何一层拿到结果就返回；全挂了才返回失败，由上层放弃注入。
# ======================================================================
async def search(
    query: str,
    api_key: str,
    count: int = 5,
    timeout: float = 20.0,
    ori_question: str = "",
    recency: bool = False,
    baidu_key: str = "",
    tavily_key: str = "",
    foreign: bool = False,
    proxy: str = "",
) -> SearchResult:
    """多引擎联网搜索，带降级。

    foreign=True 表示这句话的答案在国外站点上（国外游戏/英文文档/外网新闻），
    此时先走 Tavily；Tavily 挂了或没配，再退回国内链。
    """
    started = time.time()
    query = (query or "").strip()
    if not query:
        return SearchResult(error="搜索词为空", elapsed=time.time() - started)

    chain: list[tuple[str, object]] = []

    if foreign:
        if tavily_key:
            chain.append(("tavily", lambda: _search_tavily(query, tavily_key, count, timeout)))
        else:
            logger.info("search: 判定需要外网资料，但没配 Tavily Key，直接走国内链")

    if baidu_key:
        chain.append(("baidu", lambda: _search_baidu(query, baidu_key, count, timeout, recency)))
    chain.append(
        ("ddgs", lambda: _search_ddgs(query, count, min(timeout, 25.0), proxy))
    )
    if api_key:
        chain.append(
            ("deepseek", lambda: _search_deepseek(query, api_key, count, timeout, ori_question, recency))
        )

    last_error = "没有可用的搜索引擎"
    for name, fn in chain:
        try:
            result = await fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"search: {name} 抛异常 {type(exc).__name__}: {str(exc)[:100]}")
            last_error = f"{name}: {type(exc).__name__}"
            continue

        if result.ok and not result.empty:
            logger.info(
                f"search: {name} 命中 {len(result.hits)} 条 [{result.elapsed:.1f}s] {query!r}"
            )
            result.elapsed = time.time() - started
            result.query = query
            return result

        last_error = result.error or f"{name} 无结果"
        logger.info(f"search: {name} 没拿到结果（{last_error[:80]}），下一层")

    return SearchResult(
        query=query, error=last_error[:200], elapsed=time.time() - started
    )


def _hit_date_key(hit: SearchHit) -> str:
    """拿日期当排序键，没有日期的排最后。"""
    return hit.date or ""


def format_for_prompt(result: SearchResult | None, max_hits: int = 3) -> str:
    """把搜索结果格式化成给模型看的段落。空串表示不注入。

    重点：**按发布日期从新到旧排序**，并明确告诉模型「今天是哪天」。
    搜索引擎经常把几个月前的旧文章排前面，模型如果没日期意识，
    就会把「5 月的版本」当成「下个版本」说出来。
    """
    if result is None or not result.ok or result.empty:
        return ""

    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")

    # 新的排前面（没有日期的排后面，但保留）
    ordered = sorted(result.hits, key=_hit_date_key, reverse=True)

    lines = [
        f"【刚联网查到的资料（查询：{result.query}）】",
        f"今天是 {today}。以下是按发布日期从新到旧排列的结果：",
    ]
    for i, hit in enumerate(ordered[:max_hits], 1):
        body = (hit.summary or hit.snippet or "").strip()
        if not body:
            continue
        # 每条摘录截断：三条摘要加起来别超过 ~1200 字。
        # 塞一万字进去，模型反而只读开头，把真正有用的那几句晾在一边。
        if len(body) > 400:
            body = body[:400].rstrip() + "…"
        day = hit.date[:10] if hit.date else ""
        if day:
            try:
                age = (now - datetime.datetime.fromisoformat(day)).days
                mark = f"（{day}，{age} 天前）"
            except Exception:  # noqa: BLE001
                mark = f"（{day}）"
        else:
            mark = ""
        lines.append(f"{i}. {hit.title}{mark}")
        if (hit.url or "").startswith(("http://", "https://")):
            lines.append(f"   来源网页 {hit.url}")
        lines.append(f"   {body}")
    if len(lines) <= 2:
        return ""

    lines.append("用与本轮问题有关的资料接话，按原文和发布日期判断时效。证据缺口会影响当前结论时，说明具体缺哪一点。")
    return "\n".join(lines) + "\n"
