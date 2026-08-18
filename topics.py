#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户主题读取与 LLM 展开模块
============================
1. 从飞书多维表格「主题征集」表读取待处理主题
2. 调用 LLM 将主题展开为搜索关键词
3. 回写展开结果到多维表格，标记为已处理
4. 返回合并到当日采集流程的关键词列表

环境变量：
    LLM_API_KEY       LLM 服务密钥（复用 digest.py 的）
    LLM_BASE_URL      LLM 接口地址
    LLM_MODEL         模型名
"""

import os
import re
import json
import time
import requests

import bitable

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
LLM_MODEL = os.environ.get("LLM_MODEL") or "glm-4.7-flash"


def log(*a):
    print("[topics]", *a, flush=True)


def _llm_post(prompt: str, max_tokens: int = 300, temperature: float = 0.3,
              tries: int = 3) -> str:
    """调用 LLM，带 429 退避重试。"""
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
                wait = float(ra) if ra else (2.0 ** i)
                log("LLM 429 限流，等待 %ss" % wait)
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


def expand_topic_to_keywords(topic: str) -> list[str]:
    """
    用 LLM 将用户提交的主题展开为 5-10 个搜索关键词（中英文混合）。

    提示词设计要点：
    - 明确约束只返回关键词列表，不要解释
    - 引导向具体技术名/项目名/公司名展开，避免过于宽泛
    - 同时给出中英文关键词
    """
    prompt = f"""你是一个行业资讯搜索关键词展开助手。
用户提交了一个感兴趣的主题：「{topic}」

请将该主题展开为 5-10 个搜索关键词，用于在 GitHub、arXiv、RSS 等源中检索相关资讯。

要求：
1. 关键词应覆盖该主题的核心技术名称、相关开源项目名、相关公司/品牌名
2. 同时给出英文和中文关键词
3. 避免过于宽泛的词（如"汽车"、"AI"这种太泛的不行，要具体到子领域）
4. 如果主题过于模糊或无意义，返回空行
5. 只返回关键词列表，每行一个，不要编号、不要解释、不要其他内容

示例输入：「固态电池」
示例输出：
solid-state battery
solid electrolyte
oxide electrolyte
sulfide electrolyte
固态电池
固态电解质
半固态电池
QuantumScape
SES AI
清陶能源"""
    try:
        result = _llm_post(prompt, max_tokens=300, temperature=0.3)
        # 提取关键词：每行一个，去掉空行和编号前缀
        keywords = []
        for line in result.splitlines():
            line = line.strip()
            # 去掉可能的编号前缀 "1. xxx" -> "xxx"
            line = re.sub(r'^\d+[\.\)\-]\s*', '', line)
            # 去掉可能的 "- " 前缀
            line = re.sub(r'^[-•]\s*', '', line)
            line = line.strip()
            if line and len(line) <= 50:  # 关键词不会太长
                keywords.append(line)
            if len(keywords) >= 10:
                break
        log("主题「%s」展开为 %d 个关键词: %s" % (topic, len(keywords), keywords))
        return keywords
    except Exception as e:
        log("LLM 展开关键词失败:", e)
        return []


def get_user_topic_keywords(today: str) -> list[tuple[str, float]]:
    """
    完整流程：读取待处理主题 → LLM 展开 → 回写多维表格 → 返回关键词列表。

    返回 [(keyword_lower, weight), ...]，weight=1.5（用户主题优先级高于默认）
    """
    if not LLM_API_KEY:
        log("未配置 LLM_API_KEY，跳过用户主题展开")
        return []

    topics = bitable.get_pending_topics()
    if not topics:
        log("无待处理用户主题")
        return []

    all_keywords = []
    for t in topics:
        topic_text = t["topic"]
        record_id = t["record_id"]

        # LLM 展开关键词
        kws = expand_topic_to_keywords(topic_text)
        if not kws:
            log("主题「%s」展开结果为空，跳过" % topic_text)
            # 仍然标记为已处理，避免重复处理
            bitable.mark_topic_processed(record_id, "(展开结果为空)", today)
            continue

        # 回写多维表格
        kw_text = "\n".join(kws)
        ok = bitable.mark_topic_processed(record_id, kw_text, today)
        if not ok:
            log("回写主题处理状态失败，但关键词仍会使用: %s" % topic_text)

        # 加入当日关键词列表（权重 1.5，高于默认 1.0）
        for k in kws:
            all_keywords.append((k.lower(), 1.5))

    log("用户主题共展开 %d 个关键词" % len(all_keywords))
    return all_keywords
