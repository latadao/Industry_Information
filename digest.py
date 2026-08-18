#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业资讯每日摘报 -> 飞书群机器人
=================================
独立可运行的 Python 脚本，配合 GitHub Actions（或任何 cron）每天定时执行：

    1. 从 GitHub Search API / Hacker News(Algolia) 采集行业相关条目
    2. 按「相关 + 最新 + 权威 + 热度」四维打分，取 Top N（默认 5）
    3. 可选调用 LLM 生成中文摘要（默认规则摘要，零外部依赖）
    4. 组装飞书 interactive 卡片，POST 到群机器人 webhook

所有密钥走环境变量（GitHub Secrets / 本地 .env）：
    FEISHU_WEBHOOK  飞书群机器人 webhook 地址（必填，否则仅打印不推送）
    GITHUB_TOKEN    可选，Actions 自动注入；Search API 限额独立且更严（未认证 10 次/分钟，认证 30 次/分钟），已按此值强制限速
    GH_PAT          可选，个人 PAT，专用于 GitHub Search API：专属配额+受信IP，规避 Actions 共享IP的二级限流（见下）
                    Search API 优先用 GH_PAT，未设则回退 GITHUB_TOKEN
    LLM_API_KEY     可选，配置后用 LLM 生成摘要 + 语义精筛
    LLM_BASE_URL    可选，OpenAI 兼容接口，默认智谱 glm
    LLM_MODEL       可选，默认 glm-4.7-flash
    DIGEST_TOP_N    可选，每日条数，默认 10
    LOOKBACK_DAYS   可选，回看最近几天，默认 1
    SEEN_RETENTION_DAYS  可选，去重记录保留天数，默认 5（超过则从 seen.json 清理，允许重新推送）

零重依赖：仅 requests。
"""

import os
import sys
import json
import math
import time
import hmac
import hashlib
import base64
import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import requests
except ImportError:
    sys.exit("缺少 requests，请先: pip install requests")

HERE = Path(__file__).resolve().parent
KEYWORDS_FILE = HERE / "digest_keywords.json"
SEEN_FILE = HERE / "seen.json"

# ---------- 配置（来自环境变量） ----------
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")   # 飞书机器人签名校验密钥（建议开启）
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_PAT = os.environ.get("GH_PAT", "")            # 个人 PAT，专用于 GitHub Search API（提额+避 Actions 共享IP二级限流）
SEARCH_TOKEN = GH_PAT or GITHUB_TOKEN            # Search API 优先用 GH_PAT，未设则回退 GITHUB_TOKEN
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")

LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
LLM_MODEL = os.environ.get("LLM_MODEL") or "glm-4.7-flash"
LLM_CALL_INTERVAL = float(os.environ.get("LLM_CALL_INTERVAL", "1.5"))  # 相邻两次 LLM 调用最小间隔(秒)，缓解免费模型 RPM 限流
TOP_N = int(os.environ.get("DIGEST_TOP_N", "3"))
PAGES_URL = os.environ.get("PAGES_URL", "")
BITABLE_FORM_URL = os.environ.get("BITABLE_FORM_URL", "")  # 多维表格「主题征集」表单链接
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))
INIT_LOOKBACK_DAYS = int(os.environ.get("INIT_LOOKBACK_DAYS", "90"))  # 首次运行（seen.json 不存在）回看天数，用于历史回填
SEEN_RETENTION_DAYS = int(os.environ.get("SEEN_RETENTION_DAYS", "5"))  # 已推送记录保留天数，过期自动清理

GH_HEADERS = {"Accept": "application/vnd.github+json"}
if SEARCH_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {SEARCH_TOKEN}"

# GitHub Search API 限额远低于 core API：未认证 10 次/分钟，认证 30 次/分钟
# （核心 API 的 60/5000 次/小时不适用于 /search/*），按此设置查询间隔，避免连续 70+ 次关键词查询触发限流
# 有 Search 令牌时默认 3.0s（比原 2.2 略大，缓解二级限流）；无令牌走匿名 6.2s
GITHUB_SEARCH_INTERVAL = float(os.environ.get("GITHUB_SEARCH_INTERVAL", "3.0" if SEARCH_TOKEN else "6.2"))
_last_gh_search_ts = [0.0]


def _github_throttle():
    """确保相邻两次 GitHub Search 请求间隔 >= GITHUB_SEARCH_INTERVAL 秒。"""
    wait = GITHUB_SEARCH_INTERVAL - (time.time() - _last_gh_search_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_gh_search_ts[0] = time.time()


def log(*a):
    print("[digest]", *a, flush=True)


# ---------- 网络请求：带退避重试 ----------
def _http_get(url, params=None, headers=None, timeout=20, tries=2):
    """GET 请求，失败/超时重试 tries 次（默认 2 次 = 1 次初始 + 1 次重试）。403 不重试，多为限额/鉴权。"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 403:
                return r
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if i < tries - 1:
                log("请求失败 %d/%d，%ds 后重试: %s" % (i + 1, tries - 1, 3 * (i + 1), url[:60]))
                time.sleep(3 * (i + 1))
    raise last


def _parse_feed(url_or_text, timeout=20, tries=2):
    """RSS / arXiv 的 feedparser 解析，网络失败时重试。"""
    import feedparser
    last = None
    for i in range(tries):
        try:
            text = url_or_text
            # feedparser.parse 的 timeout 参数在新旧版本不一致，统一先用 requests 拉取
            if isinstance(text, str) and text.startswith(("http://", "https://")):
                text = _http_get(text, timeout=timeout, tries=1).text
            d = feedparser.parse(text)
            if d.bozo and not d.entries:
                raise ValueError(str(d.get("bozo_exception", "feed parse error")))
            return d
        except Exception as e:
            last = e
            if i < tries - 1:
                log("feed 解析失败 %d/%d，%ds 后重试: %s" % (i + 1, tries - 1, 3 * (i + 1), str(url_or_text)[:60]))
                time.sleep(3 * (i + 1))
    raise last


# ---------- 关键词 ----------
def load_keywords():
    if not KEYWORDS_FILE.exists():
        log("未找到 digest_keywords.json，使用内置最小关键词")
        return {"topics": [{"name": "CAD", "weight": 1.0,
                             "keywords": ["build123d", "CadQuery", "text-to-CAD"]}]}
    return json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))


def flatten_kw(kw):
    """展开为 [(关键词小写, 权重), ...]，含 brands 与 track_projects。"""
    flat = []
    for t in kw.get("topics", []):
        w = float(t.get("weight", 1.0))
        for k in t.get("keywords", []):
            flat.append((k.lower(), w))
        for b in t.get("brands", []):
            flat.append((b.lower(), w))
    for p in kw.get("track_projects", []):
        flat.append((p.lower(), 1.0))
    return flat


# ---------- 时间解析 ----------
def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------- 采集：GitHub ----------
class GitHubRateLimited(Exception):
    """GitHub 搜索额度耗尽（剩余=0），用于短路整路采集，避免空耗请求。"""
    pass


def github_search(query, since_iso):
    url = "https://api.github.com/search/repositories"
    params = {"q": f"{query} created:>{since_iso}",
              "sort": "stars", "order": "desc", "per_page": 10}
    _github_throttle()   # Search API 限额远低于 core API，请求间强制限速
    try:
        r = _http_get(url, params=params, headers=GH_HEADERS, timeout=20, tries=2)
        if r.status_code in (403, 429):
            remain = r.headers.get("x-ratelimit-remaining", "?")
            reset = r.headers.get("x-ratelimit-reset", "?")
            retry_after = r.headers.get("retry-after")
            # 剩余额度=0 -> 整点/整小时冷却，抛出短路异常，不再重试任何关键词
            if str(remain) == "0":
                raise GitHubRateLimited(f"剩余额度=0 reset={reset}")
            if retry_after:
                # 二级限流（secondary rate limit）：按其要求的秒数等待后再继续，不跳过该词
                log("GitHub 二级限流(%s)，等待 %s 秒后重试: %s" % (r.status_code, retry_after, query))
                time.sleep(float(retry_after))
                r = _http_get(url, params=params, headers=GH_HEADERS, timeout=20, tries=1)
                r.raise_for_status()
                return r.json().get("items", [])
            log("GitHub 单查询限流(%s)，跳过该词 | 剩余额度=%s" % (r.status_code, remain))
            return []
        r.raise_for_status()
        return r.json().get("items", [])
    except GitHubRateLimited:
        raise
    except Exception as e:
        log("GitHub 采集失败:", query, e)
        return []


def collect_github(kw, since_iso):
    items, seen_repo = [], set()
    flat = flatten_kw(kw)
    total = len(flat)
    log("GitHub 开始采集，共 %d 个关键词（间隔 %.1fs）" % (total, GITHUB_SEARCH_INTERVAL))
    try:
        for idx, (k, w) in enumerate(flat, 1):
            # GitHub 对纯中文仓库搜索命中低，仍保留以覆盖中文项目
            hits = github_search(k, since_iso)
            log("[GitHub] %d/%d 查 '%s' -> %d 条" % (idx, total, k, len(hits)))
            for it in hits:
                rid = it["html_url"]
                if rid in seen_repo:
                    continue
                seen_repo.add(rid)
                items.append({
                    "source": "GitHub",
                    "title": it["full_name"],
                    "url": rid,
                    "desc": it.get("description") or "",
                    "score_meta": it.get("stargazers_count", 0),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("pushed_at") or it.get("updated_at"),
                    "kw_hit": k, "kw_weight": w,
                })
    except GitHubRateLimited as e:
        log("GitHub 搜索额度已耗尽，整路跳过（%s）。其余源不受影响。" % e)
    log("GitHub 采集结束，累计 %d 条" % len(items))
    return items


# ---------- 采集：Hacker News (Algolia, 免费无需 key) ----------
def collect_hn(kw, since_ts):
    items, seen_id = [], set()
    for (k, w) in flatten_kw(kw):
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {"query": k, "tags": "story",
                  "numericFilters": f"created_at_i>{since_ts}", "hitsPerPage": 20}
        try:
            r = _http_get(url, params=params, timeout=20, tries=2)
            for h in r.json().get("hits", []):
                hid = h.get("objectID")
                if not hid or hid in seen_id:
                    continue
                seen_id.add(hid)
                items.append({
                    "source": "HackerNews",
                    "title": h.get("title") or h.get("story_title") or "",
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={hid}",
                    "desc": "",
                    "score_meta": h.get("points", 0) or 0,
                    "created_at": h.get("created_at"),
                    "updated_at": h.get("created_at"),
                    "kw_hit": k, "kw_weight": w,
                })
        except Exception as e:
            log("HN 采集失败（跳过该词，不重试）:", k, e)
    return items


# ---------- 采集：RSS（feedparser，免费无需 key） ----------
def collect_rss(kw, since_ts):
    try:
        import feedparser
    except ImportError:
        log("缺少 feedparser，跳过 RSS 采集（pip install feedparser）")
        return []
    items, seen_url = [], set()
    for feed in kw.get("rss_feeds", []):
        src = feed.get("name", "RSS")
        w = float(feed.get("weight", 1.0))
        kws = [k.lower() for k in feed.get("keywords", [])]
        try:
            d = _parse_feed(feed["url"], timeout=20, tries=2)
        except Exception as e:
            log("RSS 采集失败（已重试）:", src, e)
            continue
        for e in d.entries:
            url = e.get("link") or ""
            if not url or url in seen_url:
                continue
            title = e.get("title", "")
            summary = e.get("summary", "") or e.get("description", "")
            # 关键词命中（标题 + 摘要，按源语言原生匹配）
            hit = ""
            blob = (title + " " + summary).lower()
            for k in kws:
                if k in blob:
                    hit = k
                    break
            if not hit:
                continue
            seen_url.add(url)
            pub = e.get("published") or e.get("updated") or ""
            pub_iso = _rss_to_iso(pub)
            pub_ts = parse_ts(pub_iso)
            if pub_ts and pub_ts < since_ts:
                continue
            items.append({
                "source": f"RSS·{src}",
                "title": title,
                "url": url,
                "desc": _strip_html(summary)[:300],
                "score_meta": 0,                 # RSS 无天然热度，靠权重+新鲜度
                "created_at": _rss_to_iso(pub),
                "updated_at": _rss_to_iso(pub),
                "kw_hit": hit, "kw_weight": w,
            })
    return items


def _strip_html(s):
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _rss_to_iso(s):
    if not s:
        return None
    try:
        import email.utils as eut
        dt = eut.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


# ---------- 采集：Reddit（公开 .json 搜索，免费无需 key） ----------
def collect_reddit(kw, since_ts):
    items, seen_id = [], set()
    failed = 0   # 整路失败计数，用于一次性汇总，避免逐词刷屏
    for sub in kw.get("reddit_subreddits", []):
        sname = sub.get("name", "reddit")
        w = float(sub.get("weight", 1.0))
        kws = [k.lower() for k in sub.get("keywords", [])]
        subreddit = sname.split("/")[-1]
        # 每个板块只发 1 次请求：用 OR 拼接所有关键词一次搜完（减少请求数、避免刷屏）
        query = " OR ".join(kws) if kws else "cad"
        params = {"q": query, "restrict_sr": "1", "sort": "new",
                  "t": "week", "limit": 25}
        headers = {"User-Agent": "feishu-digest/1.0 (daily industry digest)"}
        # 主域名限流时回退 old.reddit.com（仍只试一次，不无限重试）
        hosts = ["www.reddit.com", "old.reddit.com"]
        r = None
        for host in hosts:
            try:
                r = _http_get(f"https://{host}/r/{subreddit}/search.json",
                              params=params, headers=headers, timeout=20, tries=2)
                if r.status_code != 429:
                    break
                log("Reddit 限流(429) %s，尝试回退域名" % host)
            except Exception:
                r = None
        if r is None or r.status_code == 429:
            log("Reddit 限流/失败，跳过 %s（不重试）" % sname)
            failed += 1
            continue
        try:
            data = r.json().get("data", {}).get("children", [])
        except Exception as e:
            log("Reddit 响应解析失败（跳过 %s）: %s" % (sname, e))
            failed += 1
            continue
        for c in data:
            d = c.get("data", {})
            rid = d.get("id")
            if not rid or rid in seen_id:
                continue
            created = d.get("created_utc", 0)
            if created and created < since_ts:
                continue
            # 记录第一个命中的关键词（用于卡片展示）
            title_blob = (d.get("title", "") + " " + d.get("selftext", "")).lower()
            hit = next((k for k in kws if k in title_blob), kws[0] if kws else "")
            seen_id.add(rid)
            items.append({
                "source": f"Reddit·{sname}",
                "title": d.get("title", ""),
                "url": f"https://www.reddit.com{d.get('permalink', '')}",
                "desc": (d.get("selftext", "") or "")[:300],
                "score_meta": d.get("score", 0) or d.get("ups", 0) or 0,
                "created_at": datetime.datetime.fromtimestamp(
                    created, datetime.timezone.utc).isoformat() if created else None,
                "updated_at": None,
                "kw_hit": hit, "kw_weight": w,
            })
    if failed:
        log("Reddit 共有 %d 个板块采集失败/跳过，已忽略（不重试）" % failed)
    return items


# ---------- 采集：arXiv（官方 API，免费无需 key） ----------
def collect_arxiv(kw, since_iso):
    items, seen_id = [], set()
    base = "http://export.arxiv.org/api/query"
    for cat in kw.get("arxiv_categories", []):
        cname = cat.get("category", "cs.CG")
        w = float(cat.get("weight", 0.9))
        kws = [k.lower() for k in cat.get("keywords", [])]
        # arXiv 不支持按时间过滤，抓取近期列表后在本地按关键词+时间筛
        params = {"search_query": f"cat:{cname}",
                  "sortBy": "submittedDate", "sortOrder": "descending",
                  "max_results": 50}
        try:
            r = _http_get(base, params=params, timeout=25, tries=2)
            d = _parse_feed(r.text, timeout=25, tries=2)
        except Exception as e:
            log("arXiv 采集失败（跳过该分类）:", cname, e)
            continue
        for e in d.entries:
            url = e.get("link") or ""
            aid = e.get("id", "")
            if not url or aid in seen_id:
                continue
            title = e.get("title", "").replace("\n", " ").strip()
            summary = e.get("summary", "").replace("\n", " ").strip()
            published = e.get("published", "")
            # 时间过滤
            pts = None
            try:
                import email.utils as eut
                pts = eut.parsedate_to_datetime(published).timestamp()
            except Exception:
                pass
            if pts and pts < parse_ts(since_iso):
                continue
            # 关键词命中（标题 + 摘要）
            hit = ""
            blob = (title + " " + summary).lower()
            for k in kws:
                if k in blob:
                    hit = k
                    break
            if not hit:
                continue
            seen_id.add(aid)
            items.append({
                "source": f"arXiv·{cname}",
                "title": title,
                "url": url,
                "desc": summary[:300],
                "score_meta": 0,
                "created_at": _rss_to_iso(published),
                "updated_at": _rss_to_iso(published),
                "kw_hit": hit, "kw_weight": w,
            })
    return items


# ---------- 评分（相关 / 最新 / 权威 / 热度） ----------
# 来源权威性（0~1）：用于「权威性」排序维度，越权威越高
SOURCE_AUTHORITY = {
    "arXiv": 1.0,        # 学术论文/预印本，研究侧最权威
    "GitHub": 0.9,       # 开源代码，社区 star 背书
    "RSS": 0.85,         # 科技/行业媒体（36氪、CarNewsChina、Engineering.com…）
    "HackerNews": 0.75,  # 技术社区讨论
    "Reddit": 0.6,       # 论坛，权威性最低
}

def source_authority(source):
    for k, v in SOURCE_AUTHORITY.items():
        if source.startswith(k):
            return v
    return 0.7

def relevance_score(it, flat_kw):
    """相关性 = 关键词权重 × (1 + 0.25×额外命中数)；零命中返回 0（不相关，不让噪声占坑）。"""
    blob = (it.get("title", "") + " " + it.get("desc", "")).lower()
    hits = sum(1 for (k, _w) in flat_kw if k and k in blob)
    if hits <= 0:
        return 0.0
    w = float(it.get("kw_weight", 1.0))
    return w * (1.0 + 0.25 * (hits - 1))

def normalize_url(u):
    """去重键：去 query/tracking 参数、去尾斜杠、小写 host，避免同源不同参数算两条。"""
    if not u:
        return u
    p = urlsplit(u)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))

def score_item(it, now_ts, flat_kw):
    rel = relevance_score(it, flat_kw)                # 相关
    ct = parse_ts(it.get("created_at")) or now_ts
    age_days = max(0.0, (now_ts - ct) / 86400.0)
    fresh = max(0.0, 1.0 - age_days / 7.0)            # 最新：7 天内线性衰减
    auth = source_authority(it.get("source", ""))      # 权威
    heat = math.log1p(it.get("score_meta", 0)) * 0.6   # 热度（stars/points 对数）
    return rel * 3.0 + fresh * 2.5 + auth * 2.0 + heat


# ---------- 摘要（可选 LLM） ----------
def _llm_post(prompt, max_tokens=80, temperature=0.0, tries=4):
    """统一 LLM 调用：自动对 429(Too Many Requests)做指数退避重试（尊重 retry-after 头），返回文本。"""
    last = None
    for i in range(tries):
        try:
            r = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": LLM_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=30,
            )
            if r.status_code == 429:
                ra = r.headers.get("retry-after")
                wait = float(ra) if ra else (2.0 ** i)   # 1,2,4,8s 指数退避
                log("LLM 429 限流，等待 %ss 后重试(%d/%d)" % (wait, i + 1, tries))
                time.sleep(wait)
                last = r
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2.0 ** i)
    if isinstance(last, Exception):
        raise last
    raise RuntimeError("LLM 调用失败")


def llm_summary(title, desc):
    if not LLM_API_KEY:
        return ""
    prompt = (
        "你是一名资深行业分析师。请将以下资讯用简体中文精炼总结，"
        "不论原文是何种语言都统一翻译成中文。\n"
        "严格按以下三点各写一句（每句不超过 35 字），用换行分隔，"
        "不要标题、不要序号、不要解释、除换行外不要其他符号：\n"
        "是什么：用一句话说清这条资讯的主体/对象是什么。\n"
        "能做什么：它带来什么能力或实际价值。\n"
        "怎么做到：核心技术/方法/实现路径是什么。\n"
        f"标题：{title}\n描述：{desc}"
    )
    try:
        return _llm_post(prompt, max_tokens=240, temperature=0.3)
    except Exception as e:
        log("LLM 摘要失败:", e)
        return ""


def build_topics_desc(kw):
    """把 digest_keywords.json 里的 topics/deprioritize 拼成 LLM 精筛用的主题描述，避免与关键词表脱钩。"""
    lines = []
    for i, t in enumerate(kw.get("topics", []), 1):
        name = t.get("name", f"主题{i}")
        sample = t.get("keywords", [])[:6] + t.get("brands", [])[:4]
        lines.append(f"{i}. {name}（如：{ '/'.join(sample) }）" if sample else f"{i}. {name}")
    topics_desc = "只关注以下主题，其它都不算相关：\n" + "\n".join(lines) + "；\n"
    deprioritize = kw.get("deprioritize", [])
    if deprioritize:
        topics_desc += "以下情况即使命中关键词也判为不相关：" + "、".join(deprioritize) + "。\n"
    return topics_desc


def _hit_kw(it, flat_kw):
    """该条目是否命中任一关键词；flat_kw 为空时退回采集时记录的 kw_hit。"""
    if flat_kw:
        blob = (it.get("title", "") + " " + it.get("desc", "")).lower()
        return any(k and k in blob for (k, _w) in flat_kw)
    return bool(it.get("kw_hit"))


def llm_relevant_batch(items, topics_desc, batch_size=15, flat_kw=None):
    """
    批量语义精筛：一次请求判断一批条目（≤batch_size），返回 {idx:(rel,reason)}。
    - 无 LLM_API_KEY：按关键词兜底（命中才保留）
    - 调用失败（如 429 限流）：本批降级按关键词兜底，而非整批放生
    """
    if not LLM_API_KEY:
        return {i: (_hit_kw(items[i], flat_kw), "无LLM·关键词兜底") for i in range(len(items))}
    out = {}
    for start in range(0, len(items), batch_size):
        chunk = items[start:start + batch_size]
        lines = ["%d. 标题：%s\n   摘要：%s" % (j + 1, it["title"], (it.get("desc") or "")[:300])
                 for j, it in enumerate(chunk)]
        prompt = (
            "你是行业资讯筛选助手。" + topics_desc +
            "以下是若干资讯，请逐条判断是否与以上主题真正相关；标题/摘要里偶然出现关键词但实质无关的，必须判为“不相关”。\n"
            + "\n".join(lines) +
            "\n请严格按行返回，每行格式：序号|相关|一句理由(≤15字) 或 序号|不相关|一句理由(≤15字)。只输出这些行，不要编号、不要其他内容。"
        )
        try:
            ans = _llm_post(prompt, max_tokens=min(60 * len(chunk) + 40, 1200), temperature=0.0)
            for line in ans.splitlines():
                parts = line.strip().split("|", 2)
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    j = int(parts[0].strip()) - 1
                    if 0 <= j < len(chunk):
                        rel = parts[1].strip().startswith("相关")
                        reason = parts[2].strip() if len(parts) > 2 else ""
                        out[start + j] = (rel, reason)
        except Exception as e:
            log("LLM 批量相关性判断失败，本批降级按关键词兜底:", e)
            for j in range(len(chunk)):
                out[start + j] = (_hit_kw(chunk[j], flat_kw), "精筛失败·关键词兜底")
    return out


# ---------- 去重（保留最近 SEEN_RETENTION_DAYS 天的推送记录，过期自动清理） ----------
def load_seen(now_ts):
    """返回 {url_norm: 推送日期(YYYY-MM-DD)}，加载时顺带丢弃超过保留期的旧记录。"""
    if not SEEN_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, list):
        # 兼容旧格式（纯 URL 列表，无日期）：视为今天推送，后续按新格式过期
        today = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).date().isoformat()
        raw = {u: today for u in raw}
    cutoff = now_ts - SEEN_RETENTION_DAYS * 86400.0
    kept = {}
    expired = 0
    for url, d in raw.items():
        ts = parse_ts(d) or now_ts
        if ts >= cutoff:
            kept[url] = d
        else:
            expired += 1
    if expired:
        log("去重记录清理：丢弃 %d 条超过 %d 天的旧记录" % (expired, SEEN_RETENTION_DAYS))
    return kept


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True),
                         encoding="utf-8")



# ---------- 飞书签名校验（防 URL 泄露后被滥用） ----------
def gen_feishu_sign(secret, timestamp):
    """飞书自定义机器人「签名校验」算法：HMAC-SHA256(Base64)。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


# ---------- 飞书卡片 ----------
def build_card(items):
    elements = []
    for it in items:
        md = f"**[{it['title']}]({it['url']})**\n> 来源：{it['source']} · 命中「{it['kw_hit']}」"
        summary = it.get("summary")
        if summary:
            for line in summary.split("\n"):
                line = line.strip()
                if line:
                    md += f"\n> {line}"
        else:
            d = _strip_html(it.get("desc", ""))[:140]
            if d:
                md += f"\n> {d}"
        elements.append({"tag": "markdown", "content": md})
        elements.append({"tag": "hr"})
    if not elements:
        elements = [{"tag": "markdown", "content": "今日无高相关资讯"}]
        # 即使无资讯，也追加底部链接
    else:
        # 在 Top3 末尾追加「查看本周全部资讯」链接
        if PAGES_URL:
            elements.append({
                "tag": "markdown",
                "content": f"📋 [查看本周全部资讯]({PAGES_URL})"
            })
            elements.append({"tag": "hr"})
        # 追加「提交期望主题」按钮
        if BITABLE_FORM_URL:
            elements.append({
                "tag": "markdown",
                "content": f"💡 [提交期望主题，明天帮你看]({BITABLE_FORM_URL})"
            })
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"📡 行业资讯每日摘报 · {datetime.date.today().isoformat()}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def push_feishu(card):
    # 脱敏打印 webhook（仅显示域名+路径前缀，不泄露 token）
    hook_hint = ""
    try:
        p = urlsplit(FEISHU_WEBHOOK)
        hook_hint = f"{p.scheme}://{p.netloc}{p.path[:20]}...（已隐藏）"
    except Exception:
        hook_hint = "（无法解析）"
    if not FEISHU_WEBHOOK:
        log("未配置 FEISHU_WEBHOOK，以打印模式运行（不推送）")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return False
    log("准备推送飞书 -> %s" % hook_hint)
    log("签名校验: %s" % ("已开启" if FEISHU_SECRET else "未开启（裸发）"))
    # 组装最终 payload；若配置了签名密钥则附加 timestamp + sign
    payload = dict(card)
    if FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = gen_feishu_sign(FEISHU_SECRET, ts)
    try:
        log("发送 POST 请求 ... (timeout=20s)")
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=20)
        log("飞书 HTTP 状态码: %d" % r.status_code)
        log("飞书响应体: %s" % r.text[:300])
        r.raise_for_status()
        data = r.json()
        if data.get("code", 0) != 0:
            log("飞书返回业务错误 code=%s msg=%s" % (data.get("code"), data.get("msg")))
            return False
        log("飞书推送成功 ✅")
        return True
    except Exception as e:
        log("飞书推送失败 ❌:", e)
        return False


# ---------- 主流程 ----------
def main():
    if not FEISHU_WEBHOOK:
        log("警告：FEISHU_WEBHOOK 未设置，将以打印模式运行")

    now = datetime.datetime.now(datetime.timezone.utc)
    now_ts = now.timestamp()
    is_first_run = not SEEN_FILE.exists()
    effective_lookback = INIT_LOOKBACK_DAYS if is_first_run else LOOKBACK_DAYS
    since_dt = now - datetime.timedelta(days=effective_lookback)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_ts = int(since_dt.timestamp())
    today = datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc).date().isoformat()

    kw = load_keywords()
    flat_kw = flatten_kw(kw)
    # =========================================================
    log("运行模式: %s | 回看 %d 天 | LLM=%s | TopN=%d" % (
        "首次(历史回填)" if is_first_run else "日常", effective_lookback,
        "开" if LLM_API_KEY else "关", TOP_N))

    # ---- 用户主题集成：读取多维表格待处理主题 → LLM 展开 → 合并关键词 ----
    user_kw_extra = []
    try:
        import topics as topics_mod
        user_kw_extra = topics_mod.get_user_topic_keywords(today)
        if user_kw_extra:
            flat_kw = flat_kw + user_kw_extra
            log("合并用户主题关键词 %d 个，总关键词 %d 个" % (len(user_kw_extra), len(flat_kw)))
    except Exception as e:
        log("用户主题集成失败（不影响主流程）:", e)

    log("开始采集，回看 %d 天" % effective_lookback)

    items = []
    gh = collect_github(kw, since_iso)
    log("[采集] 开始 HackerNews ...")
    hn = collect_hn(kw, since_ts)
    log("[采集] 开始 RSS ...")
    rss = collect_rss(kw, since_ts)
    log("[采集] 开始 Reddit ...")
    rd = collect_reddit(kw, since_ts)
    log("[采集] 开始 arXiv ...")
    ax = collect_arxiv(kw, since_iso)
    log("[采集] GitHub %d · HackerNews %d · RSS %d · Reddit %d · arXiv %d"
        % (len(gh), len(hn), len(rss), len(rd), len(ax)))
    items += gh + hn + rss + rd + ax
    for it in items:
        it["url_norm"] = normalize_url(it["url"])
    log("原始采集 %d 条" % len(items))

    seen = load_seen(now_ts)
    items = [it for it in items if it["url_norm"] not in seen]
    # 去重后按来源统计，便于核对各路实际入池情况
    from collections import Counter
    by_src = Counter(it["source"].split("·")[0] for it in items)
    log("去重后 %d 条，按来源: %s" % (len(items),
        " · ".join(f"{k}={v}" for k, v in by_src.items())))

    for it in items:
        it["_score"] = score_item(it, now_ts, flat_kw)
    # 纯按综合权重（相关×权威×新鲜×热度）降序，不强制各平台均分
    items.sort(key=lambda x: x["_score"], reverse=True)
    log("已按综合权重排序（相关×权威×新鲜×热度），本批最高 %.2f / 最低 %.2f"
        % (items[0]["_score"] if items else 0, items[-1]["_score"] if items else 0))

    # LLM 语义精筛：在高分候选中剔除"关键词误命中"的噪声
    topics_desc = build_topics_desc(kw)
    rel_pool = items[:max(TOP_N * 3, 15)]
    rel_results = llm_relevant_batch(rel_pool, topics_desc, flat_kw=flat_kw)
    filtered = []
    n_drop = 0
    for idx, it in enumerate(rel_pool):
        rel, reason = rel_results.get(idx, (True, ""))
        if rel:
            it["_rel_reason"] = reason
            filtered.append(it)
        else:
            n_drop += 1
            log("LLM 判定不相关，剔除: [%s] %s" % (reason, it["title"][:60]))
    log("LLM 语义过滤：候选 %d → 保留 %d，剔除 %d（不相关关键词误召回）"
        % (len(rel_pool), len(filtered), n_drop))
    filtered.sort(key=lambda x: x["_score"], reverse=True)
    # 硬门槛：零命中且 LLM 未明确判相关的纯噪声（偶发误召回）不许进 top
    hard = [it for it in filtered if it.get("kw_hit") or it.get("_rel_reason")]
    if len(hard) != len(filtered):
        log("硬门槛剔除 %d 条零命中且无 LLM 判据的噪声" % (len(filtered) - len(hard)))
    top = hard[:TOP_N]

    filter_label = " + LLM 语义过滤" if LLM_API_KEY else ""
    log("取 Top %d（按 相关/最新/权威 排序%s）" % (TOP_N, filter_label))
    for i, it in enumerate(top, 1):
        log("  #%d [%.2f] %s | %s | 命中「%s」%s"
            % (i, it.get("_score", 0), it["source"], it["title"][:60], it["kw_hit"],
               ("· " + it.get("_rel_reason")) if it.get("_rel_reason") else ""))

    for it in top:
        it["summary"] = llm_summary(it["title"], it.get("desc", "")) if LLM_API_KEY else ""
        time.sleep(LLM_CALL_INTERVAL)   # B：相邻 LLM 调用限频，缓解免费模型 RPM 限流

    if not top:
        log("无新条目，今日不推送")
        return

    # ---- 写入飞书多维表格 ----
    try:
        import bitable
        ok_bitable = bitable.write_daily_info(top, today)
        if ok_bitable:
            log("已写入多维表格「每日资讯记录」表")
        else:
            log("多维表格写入失败（不影响推送）")
    except Exception as e:
        log("多维表格写入异常（不影响推送）:", e)

    # ---- 生成网页并推送 gh-pages ----
    try:
        import webgen
        webgen.run_webgen(top, today)
    except Exception as e:
        log("网页生成/推送异常（不影响推送）:", e)

    card = build_card(top)
    ok = push_feishu(card)
    if ok:
        # 仅推送成功才标记为已推荐，避免推送失败当天丢条、次日可重试
        for it in top:
            seen[it["url_norm"]] = today
        save_seen(seen)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
