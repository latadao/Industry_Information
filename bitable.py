#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书多维表格 API 封装
=====================
提供以下能力：
  - 获取 tenant_access_token（应用令牌）
  - 向多维表格追加记录
  - 读取多维表格记录（按条件过滤）
  - 更新多维表格记录（回写字段）
  - 获取多维表格表单分享链接

所有密钥走环境变量：
    FEISHU_APP_ID        飞书自建应用 App ID
    FEISHU_APP_SECRET    飞书自建应用 App Secret
    BITABLE_APP_TOKEN    多维表格 App Token（URL 中的那段）
    BITABLE_INFO_TABLE_ID  「每日资讯记录」表 ID
    BITABLE_TOPIC_TABLE_ID 「主题征集」表 ID
"""

import os
import json
import time
import requests
from typing import Optional

# ---------- 环境变量 ----------
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BITABLE_APP_TOKEN = os.environ.get("BITABLE_APP_TOKEN", "")
BITABLE_INFO_TABLE_ID = os.environ.get("BITABLE_INFO_TABLE_ID", "")
BITABLE_TOPIC_TABLE_ID = os.environ.get("BITABLE_TOPIC_TABLE_ID", "")

# 飞书开放平台 API 基址
FEISHU_BASE = "https://open.feishu.cn/open-apis"

# token 缓存
_token_cache = {"token": "", "expires_at": 0.0}


def log(*a):
    print("[bitable]", *a, flush=True)


def get_tenant_access_token() -> str:
    """获取（带缓存的）tenant_access_token，有效期约 2 小时。"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        log("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，跳过多维表格操作")
        return ""

    url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", -1) != 0:
        log("获取 tenant_access_token 失败:", data)
        return ""

    token = data["tenant_access_token"]
    expire = data.get("expire", 7200)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expire
    log("获取 tenant_access_token 成功，有效期 %ds" % expire)
    return token


def _headers() -> dict:
    token = get_tenant_access_token()
    if not token:
        raise RuntimeError("无有效的 tenant_access_token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------- 追加记录 ----------
def append_records(table_id: str, records: list[dict]) -> bool:
    """
    向指定表格批量追加记录。
    records: [{"fields": {字段名: 值, ...}, ...}, ...]
    返回是否成功。
    """
    if not table_id or not records:
        return False

    token = get_tenant_access_token()
    if not token:
        return False

    url = f"{FEISHU_BASE}/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/batch_create"
    body = {"records": [{"fields": r} for r in records]}

    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code", -1) != 0:
            log("追加记录失败:", data.get("msg", ""))
            return False
        log("追加记录成功，共 %d 条" % len(records))
        return True
    except Exception as e:
        log("追加记录异常:", e)
        return False


# ---------- 读取记录 ----------
def list_records(table_id: str, filter_expr: Optional[str] = None,
                 page_size: int = 100) -> list[dict]:
    """
    读取表格记录，可选按飞书 filter 公式过滤。
    返回 [{"record_id": "...", "fields": {...}}, ...]
    """
    if not table_id:
        return []

    token = get_tenant_access_token()
    if not token:
        return []

    all_records = []
    page_token = ""
    # 使用 list 接口（非 search），search 需要 view_id 且权限更高
    base_url = f"{FEISHU_BASE}/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records"

    while True:
        params = {"page_size": min(page_size, 100)}
        if page_token:
            params["page_token"] = page_token
        # list 接口不支持 filter，读全部后在调用方过滤
        try:
            resp = requests.get(base_url, headers=_headers(), params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", -1) != 0:
                log("读取记录失败:", data.get("msg", ""))
                break

            items = data.get("data", {}).get("items", [])
            all_records.extend(items)
            if not data.get("data", {}).get("has_more"):
                break
            page_token = data["data"].get("page_token", "")
            if not page_token:
                break
        except Exception as e:
            log("读取记录异常:", e)
            break

    log("读取表 %s 记录 %d 条" % (table_id[:8], len(all_records)))
    return all_records


# ---------- 更新记录 ----------
def update_record(table_id: str, record_id: str, fields: dict) -> bool:
    """更新单条记录的指定字段。"""
    if not table_id or not record_id:
        return False

    token = get_tenant_access_token()
    if not token:
        return False

    url = f"{FEISHU_BASE}/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{table_id}/records/{record_id}"

    try:
        resp = requests.put(url, headers=_headers(), json={"fields": fields}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code", -1) != 0:
            log("更新记录失败:", data.get("msg", ""))
            return False
        return True
    except Exception as e:
        log("更新记录异常:", e)
        return False


# ---------- 业务封装 ----------

def write_daily_info(items: list[dict], push_date: str) -> bool:
    """
    将 Top3 资讯写入「每日资讯记录」表。
    items: digest.py 中的 top 列表（每项含 title/source/url/kw_hit/summary/_score）
    push_date: "2026-08-18"
    """
    records = []
    for i, it in enumerate(items, 1):
        item_id = f"{push_date.replace('-', '')}-{i:03d}"
        fields = {
            "日期": push_date,
            "标题": it.get("title", ""),
            "来源": it.get("source", "").split("·")[0],
            "链接": it.get("url", ""),
            "命中关键词": it.get("kw_hit", ""),
            "摘要": it.get("summary", "") or it.get("desc", "")[:200],
            "综合评分": round(it.get("_score", 0), 2),
            "点赞数": 0,
            "资讯ID": item_id,
        }
        records.append(fields)
        it["_item_id"] = item_id  # 回写到对象上，供后续使用

    return append_records(BITABLE_INFO_TABLE_ID, records)


def get_pending_topics() -> list[dict]:
    """
    读取「主题征集」表中状态为「待处理」的记录。
    list 接口不支持 filter，读全部后在本地过滤。
    返回 [{"record_id": "...", "topic": "用户输入的主题"}, ...]
    """
    raw = list_records(BITABLE_TOPIC_TABLE_ID)
    topics = []
    for r in raw:
        fields = r.get("fields", {})
        # 检查状态字段是否为"待处理"
        status_val = fields.get("状态", "")
        if isinstance(status_val, str):
            status_str = status_val
        elif isinstance(status_val, list) and status_val:
            status_str = status_val[0].get("text", "") if isinstance(status_val[0], dict) else str(status_val[0])
        else:
            status_str = str(status_val)
        if status_str != "待处理":
            continue

        topic_text = ""
        # 期望主题字段可能是文本类型
        val = fields.get("期望主题", "")
        if isinstance(val, str):
            topic_text = val.strip()
        elif isinstance(val, list) and val:
            # 飞书文本类型字段返回 [{"text": "..."}] 结构
            topic_text = val[0].get("text", "") if isinstance(val[0], dict) else str(val[0])

        if topic_text:
            topics.append({
                "record_id": r.get("record_id", ""),
                "topic": topic_text,
            })
    log("待处理主题 %d 条" % len(topics))
    return topics


def mark_topic_processed(record_id: str, keywords: str, process_date: str) -> bool:
    """将主题征集记录标记为已处理，回写展开关键词和处理日期。"""
    return update_record(
        BITABLE_TOPIC_TABLE_ID,
        record_id,
        {
            "状态": "已处理",
            "展开关键词": keywords,
            "处理日期": process_date,
        }
    )


def get_weekly_likes() -> dict:
    """
    从「每日资讯记录」表读取所有记录的点赞数。
    返回 {资讯ID: 点赞数}
    """
    records = list_records(BITABLE_INFO_TABLE_ID)
    likes = {}
    for r in records:
        fields = r.get("fields", {})
        item_id = fields.get("资讯ID", "")
        like_count = fields.get("点赞数", 0)
        if isinstance(item_id, str) and item_id:
            likes[item_id] = int(like_count) if like_count else 0
    return likes
