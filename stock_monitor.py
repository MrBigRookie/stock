#!/usr/bin/env python3
"""
财经资讯分析 & 潜力股挖掘工具
数据源：AKShare（免费开源）
推送：Bark（iOS）

用法：
  source .venv/bin/activate
  python stock_monitor.py              # 单次运行
  python stock_monitor.py --schedule   # 定时运行（默认交易日下午15:30）
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

import requests
import pandas as pd

try:
    import akshare as ak
except ImportError:
    print("请先安装 akshare: uv pip install akshare")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# ── 配置 ──────────────────────────────────────────────
CONFIG = {
    "bark_key": os.getenv("BARK_KEY", ""),
    "bark_url": os.getenv("BARK_URL", "https://api.day.app"),
    "news_days": int(os.getenv("NEWS_DAYS", "3")),
    "top_n": int(os.getenv("TOP_N", "15")),
    "schedule_time": os.getenv("SCHEDULE_TIME", "15:30"),
}

# 板块 -> 关键词列表
KEYWORDS = {
    "新能源": [
        "新能源", "光伏", "锂电", "锂电池", "储能", "风电", "氢能", "钠离子",
        "固态电池", "钙钛矿", "充电桩", "特高压", "逆变器", "宁德时代", "比亚迪",
        "隆基绿能", "通威股份", "阳光电源", "亿纬锂能", "天齐锂业", "赣锋锂业",
        "晶澳科技", "天合光能", "TCL中环", "先导智能", "恩捷股份", "璞泰来",
        "鹏辉能源", "国轩高科", "欣旺达", "德方纳米", "当升科技", "容百科技",
    ],
    "AI/科技": [
        "人工智能", "AI", "大模型", "算力", "GPU", "芯片", "半导体", "光刻机",
        "自动驾驶", "机器人", "具身智能", "Sora", "ChatGPT", "Copilot",
        "寒武纪", "海光信息", "中芯国际", "中科曙光", "浪潮信息", "工业富联",
        "科大讯飞", "商汤", "百度", "华为", "昆仑万维", "360",
        "中际旭创", "新易盛", "天孚通信", "沪电股份", "胜宏科技",
    ],
    "资金面": [
        "北向资金", "外资", "沪股通", "深股通", "净流入", "净买入",
    ],
}

# 涨停板/龙虎榜行业过滤词
ZT_FILTER = [
    "半导体", "芯片", "光刻机", "AI", "人工智能", "算力", "机器人",
    "光伏", "锂电", "锂电池", "储能", "风电", "氢能", "充电桩", "新能源",
    "光模块", "CPO", "PCB", "服务器", "数据中心",
]

NEGATIVE_WORDS = [
    "下跌", "暴跌", "跌停", "亏损", "减持", "暴雷", "退市", "处罚", "违规",
    "诉讼", "债务违约", "业绩下滑", "预亏", "风险提示", "监管", "问询函",
]
POSITIVE_WORDS = [
    "上涨", "涨停", "增长", "突破", "创新高", "增持", "盈利", "超预期",
    "利好", "中标", "签约", "量产", "交付", "扩产", "投资", "战略合作",
]

SCRIPT_DIR = Path(__file__).parent


# ── 工具函数 ──────────────────────────────────────────

def _safe_call(fn, label, retries=2, **kwargs):
    """安全调用 API，支持重试"""
    for attempt in range(retries + 1):
        try:
            result = fn(**kwargs)
            if result is None or (hasattr(result, 'empty') and result.empty):
                return None
            return result
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
                continue
            print(f"  [WARN] {label} 获取失败: {e}")
            return None


def match_keywords(text, keywords):
    return [kw for kw in keywords if kw in text]


def simple_sentiment(text):
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    if pos > neg:
        return "positive", pos - neg
    elif neg > pos:
        return "negative", neg - pos
    return "neutral", 0


# ── 数据采集 ──────────────────────────────────────────

def fetch_news():
    """采集财联社 + 东方财富新闻"""
    all_news = []

    df = _safe_call(ak.stock_info_global_cls, "财联社电报")
    if df is not None:
        for _, row in df.iterrows():
            date_str = str(row.get("发布日期", ""))
            time_str = str(row.get("发布时间", ""))
            all_news.append({
                "title": str(row.get("标题", "")),
                "content": str(row.get("内容", "")),
                "time": f"{date_str} {time_str}".strip(),
                "source": "财联社",
            })

    df = _safe_call(ak.stock_info_global_em, "东方财富快讯")
    if df is not None:
        for _, row in df.iterrows():
            all_news.append({
                "title": str(row.get("标题", "")),
                "content": str(row.get("摘要", "")),
                "time": str(row.get("发布时间", "")),
                "source": "东方财富",
            })

    return all_news


def fetch_north_flow():
    """北向资金近期流向"""
    result = {"daily": None, "summary": ""}
    df = _safe_call(ak.stock_hsgt_hist_em, "北向资金历史", symbol="北向资金")
    if df is None or df.empty:
        return result

    recent = df.tail(10)
    result["daily"] = recent

    net_col = "当日成交净买额"
    if net_col in recent.columns and recent[net_col].notna().any():
        valid = recent[net_col].dropna()
        if len(valid) >= 3:
            total = valid.tail(5).sum()
            direction = "净流入" if total > 0 else "净流出"
            result["summary"] = f"北向近5日{direction}{abs(total / 1e8):.1f}亿"
            return result

    hold_col = "持股市值"
    if hold_col in recent.columns and recent[hold_col].notna().any():
        valid = recent[hold_col].dropna()
        if len(valid) >= 2:
            chg = valid.iloc[-1] - valid.iloc[-min(6, len(valid))]
            direction = "增" if chg > 0 else "减"
            result["summary"] = f"北向持仓估值变动{direction}{abs(chg / 1e8):.0f}亿"

    return result


def fetch_north_summary():
    """北向资金当日汇总"""
    df = _safe_call(ak.stock_hsgt_fund_flow_summary_em, "北向资金汇总")
    if df is None or df.empty:
        return None, ""
    north_df = df[df["资金方向"] == "北向"] if "资金方向" in df.columns else df
    parts = []
    total_net = 0
    for _, row in north_df.iterrows():
        board = row.get("板块", "")
        amount = row.get("成交净买额", 0)
        if pd.isna(amount):
            amount = 0
        total_net += amount
        parts.append(f"{board} {amount/1e8:+.1f}亿")
    summary = " | ".join(parts) if parts else ""
    return north_df, summary


def fetch_market_fund_flow():
    """全市场资金流向"""
    df = _safe_call(ak.stock_market_fund_flow, "市场资金流向", retries=3)
    if df is None or df.empty:
        return None
    latest = df.iloc[-1]
    net = latest.get("主力净流入-净额", 0)
    super_large = latest.get("超大单净流入-净额", 0)
    large = latest.get("大单净流入-净额", 0)
    direction = "流入" if net > 0 else "流出"
    return {
        "date": str(latest.get("日期", "")),
        "net_flow": float(net) if pd.notna(net) else 0,
        "super_large": float(super_large) if pd.notna(super_large) else 0,
        "large": float(large) if pd.notna(large) else 0,
        "summary": f"主力{direction}{abs(net / 1e8):.1f}亿",
    }


def fetch_zt_pool(date_str=None):
    """涨停板数据，过滤新能源/AI相关"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    df = _safe_call(ak.stock_zt_pool_em, "涨停板", retries=3, date=date_str)
    if df is None or df.empty:
        return [], None

    # 过滤相关行业
    relevant = []
    for _, row in df.iterrows():
        industry = str(row.get("所属行业", ""))
        name = str(row.get("名称", ""))
        code = str(row.get("代码", ""))
        # 检查行业和名称是否匹配目标关键词
        text = industry + name
        matched = False
        matched_kw = ""
        for kw in ZT_FILTER:
            if kw in text:
                matched = True
                matched_kw = kw
                break
        if matched:
            relevant.append({
                "code": code,
                "name": name,
                "industry": industry,
                "change": float(row.get("涨跌幅", 0)) if pd.notna(row.get("涨跌幅")) else 0,
                "limit_times": int(row.get("连板数", 1)) if pd.notna(row.get("连板数", "")) else 1,
                "amount": float(row.get("成交额", 0)) if pd.notna(row.get("成交额")) else 0,
                "keyword": matched_kw,
            })

    stats = {
        "total": len(df),
        "relevant": len(relevant),
        "max_lianban": max((r["limit_times"] for r in relevant), default=0),
    }
    return relevant, stats


def fetch_lhb(date_str=None):
    """龙虎榜数据，筛选机构买入且与新能源/AI相关"""
    if date_str is None:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")
    else:
        end_date = date_str
        start_date = (datetime.now() - timedelta(days=3)).strftime("%Y%m%d")

    df = _safe_call(ak.stock_lhb_detail_em, "龙虎榜", retries=3, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return []

    # 筛选：机构买入 + 相关行业
    relevant = []
    for _, row in df.iterrows():
        reason = str(row.get("上榜原因", ""))
        jg_str = str(row.get("解读", ""))
        name = str(row.get("名称", ""))
        code = str(row.get("代码", ""))
        net_buy = row.get("龙虎榜净买额", 0)
        if pd.isna(net_buy):
            net_buy = 0
        net_buy = float(net_buy)

        # 过滤条件：机构参与 或 净买入 > 1000万
        has_jigou = "机构" in jg_str
        if not has_jigou and net_buy < 10_000_000:
            continue

        relevant.append({
            "code": code,
            "name": name,
            "reason": reason,
            "jiedu": jg_str,
            "net_buy": net_buy,
            "change": float(row.get("涨跌幅", 0)) if pd.notna(row.get("涨跌幅")) else 0,
            "date": str(row.get("上榜日", "")),
        })

    # 按净买入排序
    relevant.sort(key=lambda x: x["net_buy"], reverse=True)
    return relevant[:20]


def fetch_stock_fund_flow(code, name):
    """获取个股近5日资金流向"""
    market = "sh" if code.startswith("6") else "sz"
    symbol = code.replace("SH", "").replace("SZ", "").replace("sh", "").replace("sz", "")
    df = _safe_call(ak.stock_individual_fund_flow, f"个股资金-{name}", stock=symbol, market=market)
    if df is None or df.empty:
        return None
    recent = df.tail(5)
    net = recent["主力净流入-净额"].sum() if "主力净流入-净额" in recent.columns else 0
    main_pct = recent["主力净流入-净占比"].mean() if "主力净流入-净占比" in recent.columns else 0
    return {
        "net_5d": float(net) if pd.notna(net) else 0,
        "main_pct": float(main_pct) if pd.notna(main_pct) else 0,
    }


def fetch_xueqiu_hot():
    """雪球社区热门股票（讨论热度和关注度），过滤新能源/AI相关"""
    df = _safe_call(ak.stock_hot_tweet_xq, "雪球热帖", retries=2, symbol="最热门")
    if df is None or df.empty:
        return [], []

    # 所有目标股票名称集合
    target_names = set()
    for kws in KEYWORDS.values():
        for kw in kws:
            if len(kw) >= 3:
                target_names.add(kw)

    hot_relevant = []
    for _, row in df.iterrows():
        name = str(row.get("股票简称", ""))
        code = str(row.get("股票代码", ""))
        hot_count = row.get("关注", 0)
        if pd.isna(hot_count):
            hot_count = 0
        hot_count = int(hot_count)

        # 匹配目标公司
        matched = any(kw in name for kw in target_names)
        if matched:
            hot_relevant.append({
                "code": code,
                "name": name,
                "hot_count": hot_count,
            })

    # 按热度排序
    hot_relevant.sort(key=lambda x: x["hot_count"], reverse=True)

    # Top 10 雪球热门（不限行业）
    top10 = []
    for _, row in df.head(10).iterrows():
        top10.append({
            "name": str(row.get("股票简称", "")),
            "hot": int(row.get("关注", 0)) if pd.notna(row.get("关注")) else 0,
        })

    return hot_relevant[:15], top10


def fetch_caixin_news():
    """财新网精选新闻，过滤新能源/AI关键词，带原文链接"""
    df = _safe_call(ak.stock_news_main_cx, "财新网新闻")
    if df is None or df.empty:
        return []

    relevant = []
    for _, row in df.iterrows():
        tag = str(row.get("tag", ""))
        summary = str(row.get("summary", ""))
        url = str(row.get("url", ""))
        text = tag + " " + summary

        # 关键词匹配
        matched_kws = []
        for sector, kws in KEYWORDS.items():
            hits = match_keywords(text, kws)
            if hits:
                matched_kws.extend(hits)

        if matched_kws:
            # 截取摘要
            short = summary[:120] + "..." if len(summary) > 120 else summary
            relevant.append({
                "tag": tag,
                "summary": short,
                "url": url,
                "keywords": list(set(matched_kws))[:5],
            })

    return relevant[:10]


# ── 分析引擎 ──────────────────────────────────────────

def extract_companies(text):
    """提取命中的公司/行业关键词（>=3字，排除资金面通用词）"""
    companies = set()
    for sector, kws in KEYWORDS.items():
        if sector == "资金面":
            continue
        for kw in kws:
            if len(kw) >= 3 and kw in text:
                companies.add(kw)
    return companies


def analyze_news(news_list):
    """新闻关键词分析"""
    company_mentions = Counter()
    company_sentiment = defaultdict(int)
    keyword_hits = Counter()
    company_titles = defaultdict(list)

    for news in news_list:
        text = news["title"] + " " + news["content"]

        all_hits = []
        for sector, kws in KEYWORDS.items():
            if sector == "资金面":
                continue
            hits = match_keywords(text, kws)
            if hits:
                all_hits.extend(hits)
                keyword_hits.update(hits)

        if not all_hits:
            continue

        companies = extract_companies(text)
        _, score = simple_sentiment(text)

        for c in companies:
            company_mentions[c] += 1
            company_sentiment[c] += score
            if len(company_titles[c]) < 5:
                company_titles[c].append(news["title"])

    scored = []
    for name, count in company_mentions.items():
        sent = company_sentiment[name]
        raw_score = count * (1 + 0.3 * sent)
        scored.append({
            "name": name,
            "mentions": count,
            "sentiment": round(sent, 1),
            "score": round(max(raw_score, 0), 2),
            "recent_titles": company_titles[name],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored, keyword_hits


# ── 输出 ──────────────────────────────────────────────

def print_report(scored, keyword_hits, north_summary, north_detail, market_flow,
                 zt_relevant, zt_stats, lhb_relevant, xq_relevant, xq_top10, cx_news, top_n):
    """终端报告"""
    print("\n" + "=" * 80)
    print(f"  财经资讯分析报告  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 80)

    # 资金面概览
    print(f"\n  ━━ 资金面 ━━")
    print(f"  [北向] {north_summary}")
    if north_detail:
        print(f"         当日 {north_detail}")
    if market_flow:
        s = market_flow["summary"]
        extra = ""
        if market_flow.get("super_large"):
            extra = f" (超大单{market_flow['super_large']/1e8:+.1f}亿)"
        print(f"  [主力] {s}{extra}")

    # 雪球社区
    if xq_top10:
        print(f"\n  ━━ 雪球社区热门股 TOP10 ━━")
        for i, item in enumerate(xq_top10, 1):
            hot_str = f"{item['hot']/1e4:.1f}万" if item['hot'] >= 10000 else str(item['hot'])
            print(f"  {i}. {item['name']:<10} 热度: {hot_str}")
    if xq_relevant:
        print(f"\n  ━━ 雪球AI/新能源热门 ━━")
        for item in xq_relevant[:6]:
            hot_str = f"{item['hot_count']/1e4:.1f}万" if item['hot_count'] >= 10000 else str(item['hot_count'])
            print(f"  {item['name']:<10} {item['code']:<10} 热度: {hot_str}")

    # 涨停板
    if zt_stats and zt_stats["total"] > 0:
        print(f"\n  ━━ 今日涨停 ━━")
        print(f"  全市场 {zt_stats['total']} 家涨停，新能源/AI相关 {zt_stats['relevant']} 家"
              f"，最高 {zt_stats['max_lianban']} 连板")
        if zt_relevant:
            print(f"  {'名称':<10} {'行业':<12} {'涨幅':<8} {'连板':<4} {'关键词'}")
            print(f"  {'-' * 50}")
            for item in zt_relevant[:10]:
                print(f"  {item['name']:<10} {item['industry']:<12} {item['change']:>6.1f}%  "
                      f"{item['limit_times']:>3}板  {item['keyword']}")

    # 龙虎榜
    if lhb_relevant:
        print(f"\n  ━━ 龙虎榜关注（近3日机构参与）━━")
        for i, item in enumerate(lhb_relevant[:8], 1):
            net_str = f"{item['net_buy']/1e8:+.2f}亿" if abs(item['net_buy']) >= 1e8 else f"{item['net_buy']/1e4:+.0f}万"
            print(f"  {i}. {item['name']:<8} {net_str:<12} {item['jiedu'][:40]}")

    # 财新网
    if cx_news:
        print(f"\n  ━━ 财新网相关文章 ━━")
        for i, item in enumerate(cx_news[:6], 1):
            kws = ",".join(item["keywords"][:3])
            print(f"  {i}. [{item['tag']}] {item['summary'][:100]}")
            print(f"     🔗 {item['url']}")
            print(f"     🏷️ {kws}")

    # 新闻分析排名
    if not scored:
        print("\n  暂无符合新能源/AI关键词的新闻数据")
        return ""

    print(f"\n  ━━ 资讯热度排行 ━━")
    hdr = f"  {'排名':<4} {'公司/关键词':<16} {'热度':<6} {'情感':<6} {'综合分':<8}"
    print(hdr)
    print("  " + "-" * 50)

    report_lines = []
    for i, item in enumerate(scored[:top_n], 1):
        sent_label = "正面" if item["sentiment"] > 0 else ("负面" if item["sentiment"] < 0 else "中性")
        line = (f"  {i:<4} {item['name']:<16} {item['mentions']:<6} "
                f"{sent_label:<6} {item['score']:<8}")
        print(line)
        report_lines.append(
            f"{i}. {item['name']} | 热度:{item['mentions']} | 情感:{sent_label} | 评分:{item['score']}")

    print("  " + "-" * 50)
    print(f"\n  共分析 {len(scored)} 个关键词相关实体\n")

    # 关键词热度
    if keyword_hits:
        print("  ━━ 关键词热度 TOP10 ━━")
        for kw, cnt in keyword_hits.most_common(10):
            print(f"    {kw}: {cnt}次")
        print()

    # 重点新闻
    for item in scored[:5]:
        if item["recent_titles"]:
            print(f"  [{item['name']}] 相关新闻:")
            for t in item["recent_titles"][:3]:
                print(f"    - {t}")
            print()

    return "\n".join(report_lines)


def export_csv(scored, zt_relevant, lhb_relevant, keyword_hits):
    """导出 CSV"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = SCRIPT_DIR / f"report_{timestamp}.csv"
    rows = []
    for i, item in enumerate(scored, 1):
        rows.append({
            "排名": i, "名称": item["name"], "热度": item["mentions"],
            "情感分": item["sentiment"], "综合评分": item["score"],
        })
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"  报告已导出: {path}")
    return path


# ── 推送 ──────────────────────────────────────────────

def push_bark(title, body):
    """Bark 推送（POST 方式避免 URL 过长）"""
    key = CONFIG["bark_key"]
    if not key:
        print("  [INFO] 未配置 BARK_KEY，跳过推送")
        return False
    url = f"{CONFIG['bark_url']}/push"
    data = {
        "device_key": key,
        "title": title,
        "body": body,
    }
    try:
        resp = requests.post(url, json=data, timeout=15)
        result = resp.json()
        if result.get("code") == 200:
            print(f"  推送成功: {title}")
            return True
        print(f"  [WARN] 推送失败: {result}")
        return False
    except Exception as e:
        print(f"  [WARN] 推送异常: {e}")
        return False


def build_push_message(scored, north_summary, north_detail, market_flow,
                       zt_relevant, zt_stats, lhb_relevant, xq_relevant, xq_top10,
                       cx_news, keyword_hits):
    """构建推送消息（精简版，适合手机屏幕）"""
    now = datetime.now().strftime("%m-%d %H:%M")
    lines = [f"财经日报 {now}", ""]

    # 资金面
    lines.append(f"资金: {north_summary}")
    if market_flow:
        lines.append(market_flow["summary"])
    lines.append("")

    # 雪球热门（精简）
    if xq_relevant:
        names = [item["name"] for item in xq_relevant[:3]]
        lines.append(f"雪球热门: {', '.join(names)}")
        lines.append("")

    # 涨停
    if zt_stats and zt_stats["total"] > 0 and zt_relevant:
        names = [item["name"] for item in zt_relevant[:4]]
        lines.append(f"涨停({zt_stats['relevant']}/{zt_stats['total']}): {', '.join(names)}")
        lines.append("")

    # 龙虎榜（精简）
    if lhb_relevant:
        nets = []
        for item in lhb_relevant[:3]:
            net = f"{item['net_buy']/1e8:.1f}亿" if abs(item['net_buy']) >= 1e8 else f"{item['net_buy']/1e4:.0f}万"
            nets.append(f"{item['name']}+{net}")
        lines.append(f"龙虎榜: {', '.join(nets)}")
        lines.append("")

    # 热度
    if scored:
        top_names = [f"{item['name']}({item['score']})" for item in scored[:5]]
        lines.append(f"热度: {', '.join(top_names)}")
        lines.append("")

    # 财新链接（最多2条）
    if cx_news:
        lines.append("财新:")
        for item in cx_news[:2]:
            lines.append(f"  {item['summary'][:60]}")
            lines.append(f"  {item['url']}")
        lines.append("")

    # 关键词
    if keyword_hits:
        top_kw = " ".join([f"{kw}({cnt})" for kw, cnt in keyword_hits.most_common(5)])
        lines.append(top_kw)

    body = "\n".join(lines)
    return body[:800] + "..." if len(body) > 820 else body


# ── 主流程 ──────────────────────────────────────────────

def run_analysis():
    """执行一次完整分析"""
    t0 = time.time()
    today = datetime.now().strftime("%Y%m%d")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 开始采集数据...")

    # 1. 新闻
    print("  -> 采集财经新闻...")
    all_news = fetch_news()
    print(f"     共 {len(all_news)} 条")

    # 2. 北向资金
    print("  -> 获取北向资金...")
    north_flow = fetch_north_flow()
    north_df, north_detail = fetch_north_summary()
    print(f"     {north_flow['summary']}")

    # 3. 市场资金流向
    print("  -> 获取市场资金流向...")
    market_flow = fetch_market_fund_flow()
    if market_flow:
        print(f"     {market_flow['summary']}")

    # 4. 涨停板
    print("  -> 获取涨停板数据...")
    zt_relevant, zt_stats = fetch_zt_pool(today)
    if zt_stats:
        print(f"     全市场 {zt_stats['total']} 家涨停，相关 {zt_stats['relevant']} 家")
    else:
        print(f"     (今日无涨停数据或非交易日)")

    # 5. 龙虎榜
    print("  -> 获取龙虎榜数据...")
    lhb_relevant = fetch_lhb(today)
    if lhb_relevant:
        print(f"     机构参与 {len(lhb_relevant)} 条记录")
    else:
        print(f"     (无相关记录)")

    # 6. 雪球社区
    print("  -> 获取雪球社区热度...")
    xq_relevant, xq_top10 = fetch_xueqiu_hot()
    if xq_relevant:
        print(f"     AI/新能源相关 {len(xq_relevant)} 只热门股")
    else:
        print(f"     (无相关数据)")

    # 7. 财新网
    print("  -> 获取财新网精选新闻...")
    cx_news = fetch_caixin_news()
    if cx_news:
        print(f"     相关文章 {len(cx_news)} 篇")
    else:
        print(f"     (无相关文章)")

    # 8. 新闻分析
    print("  -> 分析快讯...")
    scored, keyword_hits = analyze_news(all_news)

    # 9. 输出
    print_report(scored, keyword_hits, north_flow["summary"], north_detail, market_flow,
                 zt_relevant, zt_stats, lhb_relevant, xq_relevant, xq_top10, cx_news, CONFIG["top_n"])

    # 10. 导出
    export_csv(scored, zt_relevant, lhb_relevant, keyword_hits)

    # 11. 推送
    if CONFIG["bark_key"]:
        title = f"财经日报 {datetime.now().strftime('%m-%d')}"
        body = build_push_message(scored, north_flow["summary"], north_detail, market_flow,
                                  zt_relevant, zt_stats, lhb_relevant, xq_relevant, xq_top10,
                                  cx_news, keyword_hits)
        push_bark(title, body)

    elapsed = time.time() - t0
    print(f"  分析完成，耗时 {elapsed:.1f}s\n")

    return scored


def run_schedule():
    """定时调度"""
    schedule_time = CONFIG["schedule_time"]
    hour, minute = map(int, schedule_time.split(":"))

    print(f"定时模式启动，每日 {schedule_time} 执行分析")
    print(f"按 Ctrl+C 停止\n")

    run_analysis()

    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"[{now:%H:%M:%S}] 下次执行: {target:%Y-%m-%d %H:%M:%S} ({wait / 60:.0f}分钟后)")

        time.sleep(wait)
        try:
            run_analysis()
        except Exception as e:
            print(f"[ERROR] 分析异常: {e}")
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="财经资讯分析 & 潜力股挖掘")
    parser.add_argument("--schedule", action="store_true", help="定时模式，每日收盘后执行")
    parser.add_argument("--top", type=int, default=CONFIG["top_n"], help="展示 Top N")
    parser.add_argument("--no-push", action="store_true", help="禁用 Bark 推送")
    args = parser.parse_args()

    if args.top != CONFIG["top_n"]:
        CONFIG["top_n"] = args.top
    if args.no_push:
        CONFIG["bark_key"] = ""

    if args.schedule:
        run_schedule()
    else:
        run_analysis()


if __name__ == "__main__":
    main()
