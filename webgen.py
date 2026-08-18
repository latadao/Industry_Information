#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网页生成模块
============
生成本周历史资讯汇总网页（HTML + data.json），推送到 GitHub Pages。

功能：
  1. 将每日 Top3 资讯追加到 data.json
  2. 从多维表格拉取最新点赞数，更新到 data.json
  3. 生成 index.html（纯静态，读取同目录 data.json）
  4. 通过 git push 推送到 gh-pages 分支

环境变量：
    PAGES_URL         GitHub Pages 网页地址
    GH_PAGES_TOKEN    推送 gh-pages 分支用的 GitHub Token
    GITHUB_REPO       仓库名（如 username/repo）
"""

import os
import json
import time
import datetime
from pathlib import Path

import bitable

PAGES_URL = os.environ.get("PAGES_URL", "")
GH_PAGES_TOKEN = os.environ.get("GH_PAGES_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

HERE = Path(__file__).resolve().parent
DOCS_DIR = HERE / "docs"


def log(*a):
    print("[webgen]", *a, flush=True)


def _week_of(d: datetime.date) -> str:
    """返回 ISO 周标识，如 "2026-W34"。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]}"


def load_data_json() -> dict:
    """加载现有的 data.json，不存在则返回空结构。"""
    data_file = DOCS_DIR / "data.json"
    if data_file.exists():
        try:
            return json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            log("data.json 解析失败，重建")
    return {"weeks": {}}


def save_data_json(data: dict):
    """保存 data.json。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    data_file = DOCS_DIR / "data.json"
    data_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log("data.json 已保存")


def append_daily_items(items: list[dict], push_date: str):
    """
    将当日 Top3 追加到 data.json 的对应周分组中。
    items: digest.py 中的 top 列表（每项含 _item_id）
    push_date: "2026-08-18"
    """
    data = load_data_json()
    week_key = _week_of(datetime.date.fromisoformat(push_date))

    if "weeks" not in data:
        data["weeks"] = {}
    if week_key not in data["weeks"]:
        data["weeks"][week_key] = []

    # 拉取最新点赞数
    likes = bitable.get_weekly_likes()

    week_items = data["weeks"][week_key]
    existing_ids = {it.get("id") for it in week_items}

    for it in items:
        item_id = it.get("_item_id", "")
        if item_id in existing_ids:
            continue
        week_items.append({
            "id": item_id,
            "date": push_date,
            "title": it.get("title", ""),
            "source": it.get("source", ""),
            "url": it.get("url", ""),
            "summary": it.get("summary", "") or it.get("desc", "")[:200],
            "score": round(it.get("_score", 0), 2),
            "likes": likes.get(item_id, 0),
        })

    save_data_json(data)


def update_likes_from_bitable():
    """从多维表格拉取最新点赞数，更新 data.json 中所有条目的点赞数。"""
    data = load_data_json()
    likes = bitable.get_weekly_likes()
    updated = 0
    for week_items in data.get("weeks", {}).values():
        for it in week_items:
            item_id = it.get("id", "")
            if item_id in likes:
                old = it.get("likes", 0)
                it["likes"] = likes[item_id]
                if old != likes[item_id]:
                    updated += 1
    if updated:
        save_data_json(data)
        log("点赞数更新 %d 条" % updated)
    else:
        log("点赞数无变化")


def generate_index_html():
    """生成 index.html — 纯静态页面，读取同目录 data.json 渲染。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>行业资讯每周摘报</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; line-height: 1.6; }
  .container { max-width: 800px; margin: 0 auto; padding: 20px; }
  h1 { text-align: center; color: #1a1a1a; font-size: 24px; margin-bottom: 8px; }
  .subtitle { text-align: center; color: #888; font-size: 14px; margin-bottom: 24px; }
  .week-selector { text-align: center; margin-bottom: 20px; }
  .week-selector select { padding: 6px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 14px; }
  .day-group { margin-bottom: 28px; }
  .day-header { font-size: 16px; font-weight: 600; color: #2c7be5; border-bottom: 2px solid #2c7be5; padding-bottom: 6px; margin-bottom: 12px; }
  .item-card { background: #fff; border-radius: 8px; padding: 16px 20px; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .item-title { font-size: 16px; font-weight: 600; }
  .item-title a { color: #1a1a1a; text-decoration: none; }
  .item-title a:hover { color: #2c7be5; }
  .item-meta { font-size: 12px; color: #999; margin-top: 4px; }
  .item-summary { font-size: 14px; color: #555; margin-top: 8px; white-space: pre-line; }
  .item-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; }
  .like-btn { cursor: pointer; font-size: 14px; color: #666; background: none; border: 1px solid #ddd; border-radius: 20px; padding: 4px 14px; transition: all 0.2s; }
  .like-btn:hover { border-color: #2c7be5; color: #2c7be5; }
  .like-btn.liked { background: #e8f0fe; border-color: #2c7be5; color: #2c7be5; }
  .like-count { font-weight: 600; margin-left: 4px; }
  .score-badge { font-size: 12px; color: #aaa; }
  .empty { text-align: center; color: #999; padding: 40px; }
</style>
</head>
<body>
<div class="container">
  <h1>📡 行业资讯每周摘报</h1>
  <div class="subtitle">本周资讯汇总 · 点击查看每日 Top3</div>
  <div class="week-selector">
    <select id="weekSelect" onchange="switchWeek()"></select>
  </div>
  <div id="content"></div>
</div>
<script>
let allData = null;

async function loadData() {
  try {
    const resp = await fetch('data.json');
    allData = await resp.json();
    initWeekSelector();
    showCurrentWeek();
  } catch(e) {
    document.getElementById('content').innerHTML = '<div class="empty">暂无数据</div>';
  }
}

function initWeekSelector() {
  const sel = document.getElementById('weekSelect');
  const weeks = Object.keys(allData.weeks || {}).sort().reverse();
  sel.innerHTML = weeks.map(w => `<option value="${w}">${formatWeekLabel(w)}</option>`).join('');
  // 默认选最新一周
  if (weeks.length > 0) sel.value = weeks[0];
}

function formatWeekLabel(w) {
  const parts = w.split('-W');
  return parts[0] + ' 第' + parseInt(parts[1]) + '周';
}

function showCurrentWeek() {
  const sel = document.getElementById('weekSelect');
  const weekKey = sel.value;
  const items = (allData.weeks || {})[weekKey] || [];
  const container = document.getElementById('content');

  if (items.length === 0) {
    container.innerHTML = '<div class="empty">本周暂无资讯</div>';
    return;
  }

  // 按日期分组
  const byDate = {};
  items.forEach(it => {
    if (!byDate[it.date]) byDate[it.date] = [];
    byDate[it.date].push(it);
  });

  const dates = Object.keys(byDate).sort().reverse();
  let html = '';
  dates.forEach(d => {
    html += '<div class="day-group">';
    html += '<div class="day-header">📅 ' + d + '</div>';
    byDate[d].forEach(it => {
      const liked = localStorage.getItem('liked_' + it.id) ? ' liked' : '';
      html += '<div class="item-card">';
      html += '<div class="item-title"><a href="' + it.url + '" target="_blank">' + escapeHtml(it.title) + '</a></div>';
      html += '<div class="item-meta">来源：' + escapeHtml(it.source) + ' · 命中关键词</div>';
      if (it.summary) html += '<div class="item-summary">' + escapeHtml(it.summary) + '</div>';
      html += '<div class="item-footer">';
      html += '<button class="like-btn' + liked + '" onclick="toggleLike(\\'' + it.id + '\\', this)">👍 <span class="like-count">' + it.likes + '</span></button>';
      html += '<span class="score-badge">评分 ' + it.score + '</span>';
      html += '</div>';
      html += '</div>';
    });
    html += '</div>';
  });
  container.innerHTML = html;
}

function switchWeek() { showCurrentWeek(); }

function toggleLike(id, btn) {
  const key = 'liked_' + id;
  const isLiked = localStorage.getItem(key);
  if (isLiked) {
    localStorage.removeItem(key);
    btn.classList.remove('liked');
    const countEl = btn.querySelector('.like-count');
    countEl.textContent = parseInt(countEl.textContent) - 1;
  } else {
    localStorage.setItem(key, '1');
    btn.classList.add('liked');
    const countEl = btn.querySelector('.like-count');
    countEl.textContent = parseInt(countEl.textContent) + 1;
    // 点赞数据将在次日 GitHub Actions 运行时从多维表格同步
  }
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s || '';
  return div.innerHTML;
}

loadData();
</script>
</body>
</html>'''

    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    log("index.html 已生成")


def push_to_gh_pages():
    """将 docs/ 目录推送到 gh-pages 分支。"""
    if not GH_PAGES_TOKEN or not GITHUB_REPO:
        log("未配置 GH_PAGES_TOKEN / GITHUB_REPO，跳过推送 gh-pages")
        return False

    import subprocess

    repo_url = f"https://x-access-token:{GH_PAGES_TOKEN}@github.com/{GITHUB_REPO}.git"

    # 切换到 docs 目录，初始化临时 git 仓库
    docs_path = str(DOCS_DIR)
    cmds = [
        ["git", "init"],
        ["git", "config", "user.name", "github-actions"],
        ["git", "config", "user.email", "github-actions@users.noreply.github.com"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "Update weekly digest %s" % datetime.date.today().isoformat()],
        ["git", "branch", "-M", "gh-pages"],
        ["git", "remote", "add", "origin", repo_url],
        ["git", "push", "-f", "origin", "gh-pages"],
    ]

    for cmd in cmds:
        try:
            result = subprocess.run(cmd, cwd=docs_path, capture_output=True,
                                    text=True, timeout=30)
            if result.returncode != 0 and "nothing to commit" not in result.stderr:
                log("git 命令失败: %s -> %s" % (" ".join(cmd[:3]), result.stderr[:200]))
        except Exception as e:
            log("git 命令异常: %s" % e)
            return False

    log("gh-pages 推送完成")
    return True


def run_webgen(items: list[dict], push_date: str):
    """
    完整流程：追加当日数据 → 同步点赞 → 生成网页 → 推送。
    items: digest.py 中的 top 列表
    push_date: "2026-08-18"
    """
    # 1. 追加当日数据到 data.json
    append_daily_items(items, push_date)

    # 2. 同步多维表格中的最新点赞数
    update_likes_from_bitable()

    # 3. 生成 index.html
    generate_index_html()

    # 4. 推送到 gh-pages
    push_to_gh_pages()
