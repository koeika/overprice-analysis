#!/usr/bin/env python3
"""
纳指科技ETF (159509) 溢价监控 + Seatalk 提醒 + 交易成本
=====================================================
用法:
  python3 etf_monitor.py               # 运行一次分析并发送提醒
  python3 etf_monitor.py --intraday    # 盘中模式（14:30 cron 调用）
  python3 etf_monitor.py --daily       # 盘后模式（22:00 cron 调用）
  python3 etf_monitor.py --test        # 测试模式

环境变量（必填）:
  SEATALK_APP_ID       - Seatalk App ID
  SEATALK_APP_SECRET   - Seatalk App Secret

可选:
  SEATALK_USER_EMAIL   - 接收消息的用户邮箱（默认 huixia.huang@shopee.com）
  TRADE_CAPITAL        - 每笔交易资金量（默认 20000）
  COST_MODEL           - 成本模型: ideal/low/realistic/conservative

Token 管理:
  自动用 APP_ID + APP_SECRET 获取 access_token
  7200 秒过期，自动缓存到 /tmp/etf_seatalk_token.json
  过期前自动刷新

成本模型:
  ideal        - 免五+万一+无滑点 = 0.02%/笔
  low          - 免五+万1.5+0.05%滑点 = 0.08%/笔
  realistic    - 免五+万1.5+0.1%滑点 = 0.13%/笔 ★默认
  conservative - 万3+0.15%滑点+不免五 = 0.36%/笔
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone

# ====== 配置 ======
ETF_CODE = "159509"
TRADE_CAPITAL = float(os.environ.get("TRADE_CAPITAL", "20000"))
COST_MODEL = os.environ.get("COST_MODEL", "realistic")
SEATALK_APP_ID = os.environ.get("SEATALK_APP_ID", "")
SEATALK_APP_SECRET = os.environ.get("SEATALK_APP_SECRET", "")
SEATALK_USERS = os.environ.get("SEATALK_USER_EMAILS", "huixia.huang@shopee.com,jiayu.lin@shopee.com").split(",")
SEATALK_USERS = [u.strip() for u in SEATALK_USERS if u.strip()]
SEATALK_TOKEN_FILE = "/tmp/etf_seatalk_token.json"
SEATALK_EMP_CACHE = "/tmp/etf_seatalk_emp_codes.json"

# AI 分析（可选）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"
AI_ENABLED = bool(DEEPSEEK_API_KEY)

# 海外市场指数（yfinance 免费）
OVERSEAS_INDICES = {
    "纳斯达克": "^IXIC", "标普500": "^GSPC", "费城半导体": "^SOX",
    "纳指100": "^NDX",
}

# 纳指科技 ETF 前10大权重股（美股七巨头 + 半导体）
INDEX_HOLDINGS = [
    ("苹果", "AAPL", 12.0), ("微软", "MSFT", 11.0),
    ("英伟达", "NVDA", 10.0), ("博通", "AVGO", 6.0),
    ("Meta", "META", 5.0), ("亚马逊", "AMZN", 5.0),
    ("特斯拉", "TSLA", 4.0), ("谷歌", "GOOGL", 4.0),
    ("AMD", "AMD", 3.0), ("高通", "QCOM", 2.5),
]

# 历史交易记录，用于相似日匹配（自动更新）
HISTORICAL_TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_trades_159509.json")
PENDING_TRADE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_trade_159509.json")

# Seatalk API endpoints
SEATALK_AUTH_API = "https://openapi.seatalk.io/auth/app_access_token"
SEATALK_EMP_API = "https://openapi.seatalk.io/contacts/v2/get_employee_code_with_email"
SEATALK_SINGLE_API = "https://openapi.seatalk.io/messaging/v2/single_chat"
SEATALK_GROUP_API = "https://openapi.seatalk.io/messaging/v2/group_chat"
SEATALK_GROUP_ID = os.environ.get("SEATALK_GROUP_ID", "")  # 群聊ID，设置后发群聊而非私聊

COST_MODELS = {
    "ideal":        {"name": "理想（免五+万一+无滑点）", "commission": 0.010*2, "min_fee": 0, "slippage": 0.00},
    "low":          {"name": "低佣金（免五+万1.5+0.05%滑点）", "commission": 0.015*2, "min_fee": 0, "slippage": 0.05},
    "realistic":    {"name": "实际预估（免五+万1.5+0.1%滑点）", "commission": 0.015*2, "min_fee": 0, "slippage": 0.10},
    "conservative": {"name": "保守（万3+0.15%滑点）", "commission": 0.030*2, "min_fee": 10, "slippage": 0.15},
}
# ==================


def get_access_token():
    """获取或刷新 Seatalk access token（自动缓存）"""
    # 1. 检查缓存文件
    try:
        with open(SEATALK_TOKEN_FILE) as f:
            cached = json.load(f)
            # 提前 5 分钟刷新
            if cached.get("expire_at", 0) > time.time() + 300:
                return cached["token"]
    except:
        pass

    # 2. 用 App ID + Secret 获取新 token
    if not SEATALK_APP_ID or not SEATALK_APP_SECRET:
        return None

    print(f"[Token] 获取新 access_token...")
    try:
        resp = requests.post(SEATALK_AUTH_API, json={
            "app_id": SEATALK_APP_ID,
            "app_secret": SEATALK_APP_SECRET,
        }, headers={"Content-Type": "application/json"}, timeout=15)

        data = resp.json()
        if data.get("code") == 0:
            token = data["app_access_token"]
            expire_in = data.get("expire", 7200)  # 默认 2 小时
            expire_at = time.time() + expire_in

            # 缓存
            with open(SEATALK_TOKEN_FILE, "w") as f:
                json.dump({"token": token, "expire_at": expire_at}, f)

            print(f"[Token] ✅ 获取成功，有效期到 {datetime.fromtimestamp(expire_at).strftime('%H:%M:%S')}")
            return token
        else:
            print(f"[Token] ❌ 获取失败: code={data.get('code')}, msg={data.get('message', data)}")
    except Exception as e:
        print(f"[Token] ❌ 异常: {e}")

    # 3. 如果有过期缓存也返回试试
    try:
        with open(SEATALK_TOKEN_FILE) as f:
            cached = json.load(f)
            print(f"[Token] ⚠️ 使用过期缓存")
            return cached["token"]
    except:
        pass
    return None


def calc_cost():
    """计算每笔交易成本"""
    m = COST_MODELS.get(COST_MODEL, COST_MODELS["realistic"])
    pct = m["commission"]
    if m["min_fee"] > 0 and TRADE_CAPITAL > 0:
        pct = max(pct, m["min_fee"] / TRADE_CAPITAL * 100)
    total = round(pct + m["slippage"], 3)
    return {
        "name": m["name"], "commission_pct": round(pct, 3),
        "slippage": m["slippage"], "total_pct": total,
        "capital": TRADE_CAPITAL,
        "total_yuan": round(TRADE_CAPITAL * total / 100, 2),
    }


def fetch_realtime():
    """获取实时行情（华尔街见闻，失败时 fallback yfinance）"""
    # 主数据源：华尔街见闻（有 IOPV）
    url = "https://api-ddc-wscn.awtmt.com/market/real"
    params = {"fields": "prod_name,last_px,px_change,px_change_rate,high_px,low_px,open_px,preclose_px,iopv",
              "prod_code": f"{ETF_CODE}.SZ"}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        d = r.json()
        if d.get("code") == 20000:
            s = d["data"]["snapshot"].get(f"{ETF_CODE}.SZ", [])
            if len(s) >= 9:
                return {"name": s[0], "price": s[1], "change": s[2], "change_pct": s[3],
                        "high": s[4], "low": s[5], "open": s[6], "preclose": s[7], "iopv": s[8],
                        "source": "华尔街见闻"}
    except Exception as e:
        print(f"[Warn] 华尔街见闻超时: {e}")

    # Fallback：yfinance（无 IOPV，用前日 NAV 近似）
    try:
        import yfinance as yf
        t = yf.Ticker(f"{ETF_CODE}.SZ")
        h = t.history(period="2d")
        if len(h) >= 1:
            latest = h.iloc[-1]
            prev = h.iloc[-2] if len(h) >= 2 else latest
            return {
                "name": "纳指科技ETF", "price": latest['Close'],
                "change": latest['Close'] - latest['Open'],
                "change_pct": (latest['Close'] / latest['Open'] - 1) * 100,
                "high": latest['High'], "low": latest['Low'],
                "open": latest['Open'], "preclose": prev['Close'],
                "iopv": None, "source": "yfinance(无IOPV)",
            }
    except Exception as e:
        print(f"[ERROR] yfinance fallback 也失败: {e}")

    return None


def fetch_nav():
    """获取最新净值（akshare → 失败时直连东方财富API）"""
    import akshare as ak
    try:
        today = datetime.now()
        start = (today - timedelta(days=30)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        df = ak.fund_etf_fund_info_em(fund=ETF_CODE, start_date=start, end_date=end)
        if len(df) >= 2:
            L, P = df.iloc[-1], df.iloc[-2]
            return {
                "date": str(L["净值日期"]), "nav": float(L["单位净值"]),
                "chg": float(L["日增长率"]) if L["日增长率"] else 0,
                "prev_date": str(P["净值日期"]), "prev_nav": float(P["单位净值"]),
            }
        elif len(df) == 1:
            L = df.iloc[-1]
            return {"date": str(L["净值日期"]), "nav": float(L["单位净值"]),
                    "chg": float(L["日增长率"]) if L["日增长率"] else 0,
                    "prev_date": None, "prev_nav": None}
    except Exception as e:
        print(f"[Warn] akshare: {e}")

    # Fallback: 直连东方财富 API
    try:
        url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={ETF_CODE}&pageIndex=1&pageSize=10"
        r = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fundf10.eastmoney.com/"
        }, timeout=15)
        data = r.json()
        records = data.get("Data", {}).get("LSJZList", [])
        if len(records) >= 2:
            L, P = records[0], records[1]
            return {
                "date": L["FSRQ"], "nav": float(L["DWJZ"]),
                "chg": float(L.get("JZZZL", 0) or 0),
                "prev_date": P["FSRQ"], "prev_nav": float(P["DWJZ"]),
            }
        elif len(records) == 1:
            L = records[0]
            return {"date": L["FSRQ"], "nav": float(L["DWJZ"]),
                    "chg": float(L.get("JZZZL", 0) or 0),
                    "prev_date": None, "prev_nav": None}
    except Exception as e:
        print(f"[ERROR] 东方财富NAV也失败: {e}")

    return None


def analyze():
    """完整分析"""
    data = fetch_realtime()
    if not data:
        return {"error": "无法获取实时行情"}

    nav = fetch_nav()
    cost = calc_cost()
    price = data["price"]
    iopv = data.get("iopv")
    preclose = data.get("preclose", price)

    # 北京时间
    bj_time = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M 北京时间")
    r = {
        "time": bj_time,
        "price": price, "change_pct": data.get("change_pct", 0),
        "high": data["high"], "low": data["low"],
        "open": data["open"], "preclose": preclose, "iopv": iopv,
    }

    # IOPV 溢价
    if iopv and iopv > 0:
        r["iopv_premium"] = round((price - iopv) / iopv * 100, 2)

    # NAV 溢价 + Δ
    if nav and nav["nav"]:
        r["nav"] = nav["nav"]
        r["nav_date"] = nav["date"]
        r["nav_premium"] = round((price - nav["nav"]) / nav["nav"] * 100, 2)

        if nav.get("prev_nav") and preclose:
            prev_prem = (preclose - nav["prev_nav"]) / nav["prev_nav"] * 100
            cur_prem = r.get("iopv_premium") or r["nav_premium"]
            r["prev_premium"] = round(prev_prem, 2)
            r["delta"] = round(cur_prem - prev_prem, 2)

    # 信号判断
    delta = r.get("delta", 0)
    prem = r.get("iopv_premium") or r.get("nav_premium") or 0

    # 从历史数据库动态计算胜率
    trades = load_historical_trades()
    total_n = len(trades)
    total_wins = sum(1 for t in trades if t.get("result") == "win")
    dynamic_wr = f"{total_wins/total_n*100:.0f}%（{total_n}笔）" if total_n > 0 else "无数据"

    # 动态计算历史平均收益（全部交易，非写死）
    all_rets = [t.get("ret", 0) for t in trades]
    dynamic_avg = sum(all_rets) / len(all_rets) if all_rets else 0

    # 按 Δ 档位从历史数据库动态计算胜率/收益（不写死）
    def band_stats(lo, hi):
        """返回 (笔数, 赢笔数, 输笔数, 胜率%, 平均收益%)；样本不足返回 None"""
        sub = [t for t in trades if lo <= t.get("delta", 0) < hi]
        if not sub:
            return None
        w = sum(1 for t in sub if t.get("result") == "win")
        avg = sum(t.get("ret", 0) for t in sub) / len(sub)
        return len(sub), w, len(sub) - w, w / len(sub) * 100, avg

    def band_desc(name, lo, hi):
        """生成信号描述：真实胜率 + 样本量；无数据如实标注"""
        s = band_stats(lo, hi)
        if s is None:
            return f"{name}，该档位暂无历史样本"
        n, wins, losses, wr, avg = s
        return f"{name}，历史{n}笔（赢{wins}输{losses}）胜率{wr:.0f}%（均收益{avg:+.2f}%）"

    # 档位名称（推导过程用）与区间
    BAND_NAMES = {
        "strong_buy": "Δ<-3% 强烈抄底", "buy": "-3~-2% 抄底", "weak_buy": "-2~-1% 弱抄底",
    }
    BAND_RANGES = {
        "strong_buy": (-100, -3.0), "buy": (-3.0, -2.0), "weak_buy": (-2.0, -1.0),
    }

    # 159509 反向策略：Δ↓（溢价暴跌）= 抄底信号（胜率动态计算）
    signal_map = [
        (lambda d, p: d < -3.0, "strong_buy", "🟢 强烈抄底！", band_desc("溢价暴跌超3%", -100, -3.0)),
        (lambda d, p: d < -2.0, "buy", "🟢 抄底信号", band_desc("溢价大跌2%以上", -3.0, -2.0)),
        (lambda d, p: d < -1.0, "weak_buy", "🟡 弱抄底信号", band_desc("溢价回落1%以上", -2.0, -1.0)),
        (lambda d, p: d > 3.0, "danger", "🔴 溢价暴涨风险", band_desc("溢价急升超3%", 3.0, 100)),
    ]
    for cond, sig, text, desc in signal_map:
        if cond(delta, prem):
            r["signal"] = sig
            r["signal_text"] = f"{text} Δ溢价 {delta:+.2f}%"
            r["signal_conf"] = desc
            r["total_n"] = total_n
            r["total_wr_pct"] = round(total_wins / total_n * 100, 1) if total_n else 0
            if sig == "danger":
                r["est_gross"] = 0
                r["est_net"] = round(0 - cost["total_pct"], 2)
            else:
                # 预估收益优先用本档位真实平均收益；样本不足时退回全库平均
                bs = band_stats(*BAND_RANGES[sig])
                if bs:
                    n, wins, losses, wr, avg = bs
                    r["band"] = {"name": BAND_NAMES[sig], "n": n, "wins": wins,
                                 "losses": losses, "wr": wr, "avg": avg}
                est = bs[4] if bs else dynamic_avg
                r["est_gross"] = round(est, 2)
                r["est_net"] = round(est - cost["total_pct"], 2)
            r["cost"] = cost
            return r

    r["signal"] = "neutral"
    r["signal_text"] = "⚪ 无明确信号"
    r["signal_conf"] = "建议观望"
    r["est_gross"] = 0
    r["est_net"] = round(0 - cost["total_pct"], 2)
    r["cost"] = cost
    return r


def fetch_market_context():
    """获取海外市场行情 + 权重股涨跌（免费，yfinance）"""
    ctx = {"indices": {}, "holdings": []}
    try:
        import yfinance as yf
        # 大盘指数（重试3次，yfinance 偶尔限流）
        for name, symbol in OVERSEAS_INDICES.items():
            for attempt in range(3):
                try:
                    t = yf.Ticker(symbol)
                    h = t.history(period="2d")
                    if len(h) >= 2:
                        latest = h['Close'].iloc[-1]
                        prev = h['Close'].iloc[-2]
                        ctx["indices"][name] = round((latest - prev) / prev * 100, 2)
                    break
                except:
                    if attempt < 2:
                        time.sleep(1)
                    continue
        # 权重股（带重试）
        for name, ticker, weight in INDEX_HOLDINGS:
            for attempt in range(2):
                try:
                    t = yf.Ticker(ticker)
                    h = t.history(period="5d")  # 取5天避免单日缺失
                    if len(h) >= 2:
                        latest = h['Close'].iloc[-1]
                        prev = h['Close'].iloc[-2]
                        chg = round((latest - prev) / prev * 100, 2)
                        ctx["holdings"].append({
                            "name": name, "ticker": ticker, "weight": weight, "chg": chg
                        })
                    break
                except:
                    if attempt == 0:
                        time.sleep(0.5)  # 等半秒重试
                    continue
    except Exception as e:
        print(f"[Market] yfinance 获取失败: {e}")
    return ctx


def load_historical_trades():
    """加载历史交易记录（57笔）"""
    try:
        with open(HISTORICAL_TRADES_FILE) as f:
            return json.load(f)
    except:
        return _default_trades()


def _default_trades():
    """内置历史交易摘要（57笔回测结果的关键特征）"""
    return [
        {"date":"2025-01-21","delta":6.65,"premium":31.99,"ret":4.53,"vol_ratio":1.8,"trend_5d":3.5,"result":"win"},
        {"date":"2025-01-15","delta":5.87,"premium":16.71,"ret":3.16,"vol_ratio":1.5,"trend_5d":4.2,"result":"win"},
        {"date":"2025-01-08","delta":10.14,"premium":4.27,"ret":1.45,"vol_ratio":1.2,"trend_5d":2.1,"result":"win"},
        {"date":"2024-07-05","delta":8.72,"premium":6.77,"ret":2.60,"vol_ratio":1.6,"trend_5d":3.8,"result":"win"},
        {"date":"2025-01-14","delta":5.87,"premium":10.84,"ret":1.04,"vol_ratio":1.3,"trend_5d":3.0,"result":"win"},
        {"date":"2025-01-16","delta":5.69,"premium":20.57,"ret":1.07,"vol_ratio":1.4,"trend_5d":5.1,"result":"win"},
        {"date":"2026-04-30","delta":5.76,"premium":-0.19,"ret":4.48,"vol_ratio":1.3,"trend_5d":0.8,"result":"win"},
        {"date":"2024-08-05","delta":1.33,"premium":-1.78,"ret":5.00,"vol_ratio":2.0,"trend_5d":-2.5,"result":"win"},
        {"date":"2025-02-28","delta":2.40,"premium":3.65,"ret":2.71,"vol_ratio":1.6,"trend_5d":-1.2,"result":"win"},
        {"date":"2025-01-20","delta":5.74,"premium":26.25,"ret":1.99,"vol_ratio":2.1,"trend_5d":6.8,"result":"win"},
        # 失败案例
        {"date":"2025-11-04","delta":3.03,"premium":1.98,"ret":-3.20,"vol_ratio":0.8,"trend_5d":0.3,"result":"loss"},
        {"date":"2025-01-10","delta":1.47,"premium":10.61,"ret":-0.81,"vol_ratio":1.1,"trend_5d":2.5,"result":"loss"},
        {"date":"2025-02-14","delta":7.02,"premium":6.94,"ret":-0.64,"vol_ratio":0.9,"trend_5d":1.2,"result":"loss"},
        {"date":"2025-06-20","delta":1.15,"premium":-1.29,"ret":-0.15,"vol_ratio":0.7,"trend_5d":-0.5,"result":"loss"},
        {"date":"2025-03-03","delta":2.40,"premium":3.65,"ret":-2.05,"vol_ratio":0.6,"trend_5d":-1.8,"result":"loss"},
        {"date":"2024-09-27","delta":1.44,"premium":-1.20,"ret":-0.78,"vol_ratio":0.8,"trend_5d":0.2,"result":"loss"},
    ]


def find_similar_days(current, trades, top_n=3):
    """用特征向量找最相似的历史交易日"""
    import math
    features = ["delta", "premium", "trend_5d"]

    def vector(t):
        return [abs(t.get(f, 0) or 0) for f in features]

    cur_vec = vector(current)
    norm_c = sum(x*x for x in cur_vec) ** 0.5

    if norm_c == 0 or math.isnan(norm_c):
        return []

    scored = []
    for t in trades:
        t_vec = vector(t)
        # 跳过含 NaN 的记录
        if any(math.isnan(v) for v in t_vec):
            continue
        norm_t = sum(x*x for x in t_vec) ** 0.5
        if norm_t == 0 or math.isnan(norm_t):
            continue
        dot = sum(a*b for a,b in zip(cur_vec, t_vec))
        sim = dot / (norm_c * norm_t)
        if math.isnan(sim):
            continue
        scored.append({**t, "similarity": round(sim * 100)})

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_n]


def ai_analyze(analysis, mode="daily"):
    """增强 AI 分析：多市场数据 + 相似日匹配"""
    if not AI_ENABLED:
        return None
    if "error" in analysis:
        return None  # 数据获取失败，跳过 AI

    signal = analysis.get("signal", "")
    is_buy = signal in ("strong_buy", "buy", "weak_buy")

    delta = analysis.get("delta", 0)
    premium = analysis.get("iopv_premium") or analysis.get("nav_premium") or 0
    chg = analysis.get("change_pct", 0)
    amp = (analysis.get("high", 0) - analysis.get("low", 0)) / analysis.get("open", 1) * 100
    price = analysis["price"]

    # 1. 海外市场 + 权重股数据
    market_ctx = fetch_market_context()
    idx = market_ctx.get("indices", {})
    market_str = "\n".join([f"- {k}: {v:+.2f}%" for k, v in idx.items()]) if idx else "数据暂不可用"

    holdings = market_ctx.get("holdings", [])
    if holdings:
        # 按涨幅排序，取涨跌各前5
        sorted_h = sorted(holdings, key=lambda x: x["chg"], reverse=True)
        top_gainers = sorted_h[:5]
        top_losers = sorted_h[-5:]
        holdings_str = "\n".join([
            f"- {h['name']}({h['weight']}%): {h['chg']:+.2f}%"
            for h in top_gainers + top_losers
        ])
    else:
        holdings_str = "数据暂不可用"

    # 2. 相似日匹配
    trades = load_historical_trades()
    current_features = {
        "delta": abs(delta), "premium": abs(premium),
        "trend_5d": chg,  # 用当日涨跌近似
    }
    similar = find_similar_days(current_features, trades)

    sim_str = ""
    if similar:
        sim_str = "\n".join([
            f"- {s['date']}: Δ{s['delta']:.1f}% 溢价{s['premium']:.1f}% → "
            f"{'🟢赚' if s['result']=='win' else '🔴亏'}{abs(s['ret']):.2f}% "
            f"(相似度{s['similarity']}%)"
            for s in similar
        ])

    wins = sum(1 for s in similar if s["result"] == "win")
    warning = ""
    if wins < len(similar) / 2:
        warning = "⚠️ 相似历史交易日多数亏损，需谨慎！"

    # 3. 构建 prompt
    if signal == "danger":
        prompt = f"""你是量化交易分析师。请对纳指科技ETF(159509)的**溢价暴涨风险**信号做简短分析。

📊 当前数据:
- 现价: {price:.4f}，涨跌幅: {chg:+.2f}%
- IOPV溢价: {premium:.2f}%（高位）
- Δ溢价: {delta:+.2f}%（收缩中）
- 振幅: {amp:.1f}%

🌏 海外指数:
{market_str}

📌 权重股表现:
{holdings_str}

请用中文回复，严格按格式（每行一个标签）：

[概况]
<1句话，溢价水平和风险程度>

[海外]
<1句话，海外市场是否支持溢价维持>

[关注]
<1句话，建议观望还是减仓，以及关键观察点>

三行即可，每行不超过40字。"""
    elif is_buy:
        prompt = f"""你是量化交易分析师。请基于以下数据对纳指科技ETF(159509)的**买入信号**做简短分析。

📊 当前信号:
- 现价: {price:.4f}，涨跌幅: {chg:+.2f}%
- IOPV溢价: {premium:.2f}%
- Δ溢价: {delta:+.2f}%
- 信号强度: {signal}，振幅: {amp:.1f}%
- 历史胜率: {analysis.get('signal_conf', '')}

🌏 海外指数:
{market_str}

📌 权重股表现:
{holdings_str}

📜 最相似历史交易:
{sim_str}
{warning}

请用中文回复，严格按格式输出四行（每行一个标签，不超过40字）：

[置信度]
<高/中/低，一个词>

[相似度]
<1句话，与历史交易的相似度和差异>

[海外]
<1句话，海外市场是否支撑当前信号>

[风险]
<1句话，当前最大风险>

置信度判定标准：
- 高: 海外普涨+权重股多数涨+历史相似日多数盈利+溢价非极端
- 中: 部分条件满足，有正面也有负面信号
- 低: 海外偏弱+权重股多数跌+历史相似日亏损+溢价极端

推理要求（仅用于思考过程，最终回复仍是上面四行，不要在回复里输出推理步骤）：
思考中按以下步骤逐项核对，每步一两句话即可，整个思考过程控制在600字以内：
① 数据核对: 列出海外指数与权重股涨跌的关键数据
② 条件比对: 逐条比对判定标准，写出满足/不满足
③ 相似日证据: 引用最相似历史日的Δ溢价/溢价/收益
④ 最终判定: 定置信度；证据冲突时说明取舍理由"""
    else:
        prompt = f"""你是量化交易分析师。请对纳指科技ETF(159509)做简短的**日常市场扫描**（今日无买入信号）。

📊 今日概况:
- 现价: {price:.4f}，涨跌幅: {chg:+.2f}%
- IOPV溢价: {premium:.2f}%
- Δ溢价: {delta:+.2f}%（未触发买入阈值）
- 振幅: {amp:.1f}%

🌏 海外指数:
{market_str}

📌 权重股表现:
{holdings_str}

请用中文回复，严格按格式（每行一个标签）：

[概况]
<1句话，今日溢价和市场的整体状态>

[海外]
<1句话，海外市场对ETF走势的影响>

[关注]
<1句话，明日需要关注的风险或机会>

三行即可，每行不超过40字。"""

    try:
        resp = requests.post(DEEPSEEK_API, json={
            "model": "deepseek-reasoner",  # 推理模式，深度思考
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,            # 充足预算（推理token+回复共用）
            "temperature": 0.1,            # 最低温度，最专注
        }, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        }, timeout=60)  # 推理模式需要更长时间

        if resp.status_code == 200:
            data = resp.json()
            msg = data["choices"][0]["message"]
            reasoning = msg.get("reasoning_content", "")  # 推理过程
            content = msg.get("content", "").strip()      # 最终结论
            if reasoning:
                print(f"[AI] 推理过程 ({len(reasoning)}字): {reasoning[:200]}...")
                analysis["ai_reasoning"] = reasoning  # 留存推理过程，用于推导佐证
            return content or reasoning.strip()
        else:
            print(f"[AI] API 错误: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[AI] 调用失败: {e}")

    return None


def write_diag_log(analysis, mode, ai_result=None):
    """写诊断日志，每次运行追加到 run.log"""
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.log")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"\n{'='*60}",
        f"运行时间: {ts} | 模式: {mode}",
        f"行情: price={analysis.get('price','?')} chg={analysis.get('change_pct','?')}%",
        f"IOPV: {analysis.get('iopv','?')} 溢价={analysis.get('iopv_premium','?')}%",
        f"NAV: {analysis.get('nav','?')} ({analysis.get('nav_date','?')}) 溢价={analysis.get('nav_premium','?')}%",
        f"前日溢价: {analysis.get('prev_premium','?')}%",
        f"Δ溢价: {analysis.get('delta','?')}%",
        f"信号: {analysis.get('signal','?')} | {analysis.get('signal_text','?')}",
        f"AI: {'已分析' if ai_result else '未触发/未启用'}",
    ]

    if analysis.get("cost"):
        c = analysis["cost"]
        lines.append(f"成本: {c['total_pct']}%/笔 ({c['name']})")
    if analysis.get("est_net"):
        lines.append(f"预估净收益: {analysis['est_net']}%/笔")
    if ai_result:
        lines.append(f"AI分析: {ai_result}")

    lines.append("-" * 40)

    try:
        with open(log_file, "a") as f:
            f.write("\n".join(lines) + "\n")
    except:
        pass


def format_message(a):
    """格式化 Seatalk 消息（简洁版，兼容 Seatalk markdown）"""
    if "error" in a:
        return f"⚠️ ETF监控异常：{a['error']}"

    E = {"strong_buy": "🚨", "buy": "📈", "weak_buy": "📊", "danger": "⚠️", "neutral": "ℹ️"}
    emoji = E.get(a.get("signal", "neutral"), "ℹ️")
    cost = a.get("cost", {})

    m = f"""{emoji} **纳指科技ETF 溢价监控**
{a['time']}

📊 **实时行情**
• 现价: **{a['price']:.4f}** ({a.get('change_pct', 0):+.2f}%)
• IOPV: {a.get('iopv', 'N/A')}
• IOPV溢价: **{a.get('iopv_premium', 'N/A')}%**
"""

    if "nav" in a:
        m += f"• 最新NAV: {a['nav']:.4f}（{a.get('nav_date', '')}）  \n"
    if "prev_premium" in a:
        m += f"• 昨日收盘溢价: {a['prev_premium']:.2f}%  \n"
    if "delta" in a:
        m += f"• **Δ溢价变动: {a['delta']:+.2f}%**  \n"

    m += f"""
🎯 **信号: {a.get('signal_text', 'N/A')}**
{a.get('signal_conf', '')}

"""

    # AI 置信度标签（f-string 外部）
    ai_conf = a.get("ai_confidence", "")
    if ai_conf:
        conf_emoji = {"高": "🟢", "中": "🟡", "低": "🔴"}.get(ai_conf, "⚪")
        m += f"{conf_emoji} **AI 置信度: {ai_conf}**  \n"
        if ai_conf == "低":
            m += "> ⚠️ AI 建议减半仓位或观望，当前和历史成功案例偏差较大  \n"
        elif ai_conf == "中":
            m += "> ⚠️ AI 建议常规操作，注意风险控制  \n"

    m += f"""
💰 **交易成本**（{cost.get('name', 'N/A')}）
• 资金: ¥{cost.get('capital', 0):,.0f}/笔
• 佣金+滑点: **{cost.get('total_pct', 0):.3f}%**（约¥{cost.get('total_yuan', 0):.2f}）
"""

    if a.get("signal") in ("strong_buy", "buy", "weak_buy"):
        eg, en = a['est_gross'], a['est_net']
        net_yuan = en * cost.get('capital', 20000) / 100
        warn = ""
        if en < 0:
            warn = "\n> ⚠️ 该档位历史期望收益为负，不建议实盘操作\n"
        m += f"""
📈 **预估收益**
• 历史毛收益: {eg:+.2f}%/笔
• 扣除成本净收益: **{en:+.2f}%/笔** ≈ **{net_yuan:+.2f} 元**
{warn}
> 🟢 14:55 尾盘买入 → 次日 9:25 开盘卖出
> 📐 推导过程见本条消息的回复
"""

    # 已结算交易
    settled = a.get("settled_trade")
    if settled:
        e = "🟢" if settled["result"] == "win" else "🔴"
        m += f"""
📋 **上次交易结算**
• {settled['date']}: {e} {settled['ret']:+.2f}%（历史数据库已更新）
"""

    m += f"""
---
🔍 数据质量: {a.get('data_quality', '未知')}
📌 历史回测仅供参考，不构成投资建议
"""
    # 附加 AI 分析（格式化标签，提取置信度）
    ai = a.get("ai_analysis")
    if ai:
        # 提取置信度
        import re as _re
        conf_match = _re.search(r'\[置信度\]\s*(.+)', ai)
        ai_conf = conf_match.group(1).strip()[:10] if conf_match else ""
        if ai_conf:
            a["ai_confidence"] = ai_conf
            ai = ai.replace(conf_match.group(0), "")  # 移除原始行

        ai = ai.replace("[相似度]", "\n🔍 **相似度**")
        ai = ai.replace("[概况]", "\n📊 **概况**")
        ai = ai.replace("[海外]", "\n🌏 **海外**")
        ai = ai.replace("[风险]", "\n⚠️ **风险**")
        ai = ai.replace("[关注]", "\n👀 **关注**")
        ai = _re.sub(r'\n\s*\n', '\n', ai)  # 清理空行
        m += f"\n🤖 **AI 分析**{ai}\n"
    return m


def format_ai_reasoning(text):
    """把AI推理全文整理成可读格式：空行分段 + 长段按句拆行（每行≤120字）"""
    import re
    out = []
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        if len(para) <= 120:
            out.append(para)
            continue
        # 长段按句号/问号/感叹号/分号拆行
        cur = ""
        for seg in re.split(r'(?<=[。！？；])', para):
            if len(cur) + len(seg) <= 120:
                cur += seg
            else:
                if cur.strip():
                    out.append(cur.strip())
                cur = seg
        if cur.strip():
            out.append(cur.strip())
    return "\n\n".join(out)


def build_evidence(a):
    """抄底信号时生成推导过程文本，附在信号消息的线程回复中佐证结论"""
    band = a.get("band")
    if not band:
        return None
    cost = a.get("cost", {})
    capital = cost.get("capital", 20000)
    lines = [
        "📐 抄底信号推导过程（数据可核对）",
        "━━━━━━━━━━━━━",
        f"① 触发条件: Δ溢价 {a.get('delta', 0):+.2f}% 落入「{band['name']}」档",
        f"② 该档位历史样本: {band['n']}笔 = 赢{band['wins']} 输{band['losses']} → 胜率 {band['wr']:.0f}%",
        f"   该档位历史平均收益: {band['avg']:+.2f}%/笔（毛）",
        f"③ 扣成本 {cost.get('total_pct', 0)}%/笔 → 净期望 {a.get('est_net', 0):+.2f}%/笔 ≈ ¥{a.get('est_net', 0) * capital / 100:+,.2f}（¥{capital:,.0f}/笔）",
        f"④ 全库对比: {a.get('total_n', 0)}笔整体胜率{a.get('total_wr_pct', 0):.1f}%（仅供参考，非本档位）",
    ]
    # 相似日匹配（与 AI 分析同源的特征向量）
    try:
        trades = load_historical_trades()
        cur = {
            "delta": abs(a.get("delta", 0)),
            "premium": abs(a.get("iopv_premium") or a.get("nav_premium") or 0),
            "trend_5d": a.get("change_pct", 0),
        }
        similar = find_similar_days(cur, trades)
        if similar:
            lines.append("⑤ 最相似历史交易日:")
            for s in similar:
                lines.append(f"  {s['date']}: Δ{s.get('delta',0):+.1f}% → "
                             f"{'🟢赚' if s.get('result')=='win' else '🔴亏'}{abs(s.get('ret',0)):.2f}%"
                             f"（相似度{s.get('similarity',0)}%）")
    except:
        pass
    # 边际提示（按真实样本量提示风险）
    if band["wr"] < 55:
        lines.append(f"⑥ 提示: 本档胜率{band['wr']:.0f}%，建议减半仓位或等待Δ跌幅更深再介入")
    elif band["n"] < 10:
        lines.append(f"⑥ 提示: 本档仅{band['n']}笔样本，统计意义有限，轻仓为宜")
    else:
        lines.append("⑥ 提示: 历史回测仅供参考，不构成投资建议")
    # AI 推理过程（全文，整理为分段可读格式）
    reasoning = a.get("ai_reasoning", "")
    if reasoning:
        lines.append("⑦ AI推理过程(全文):")
        lines.append(format_ai_reasoning(reasoning))
    # markdown 需"两个空格+换行"才是真换行；群聊纯文本模式下无影响
    return "  \n".join(lines)


def get_employee_codes():
    """获取所有收件人的 employee_code（批量查询，自动缓存）"""
    # 1. 检查缓存
    try:
        with open(SEATALK_EMP_CACHE) as f:
            cached = json.load(f)
            # 检查是否覆盖所有用户
            if all(u in cached for u in SEATALK_USERS):
                return [cached[u] for u in SEATALK_USERS]
    except:
        pass

    # 2. 批量查询
    token = get_access_token()
    if not token:
        return SEATALK_USERS  # fallback

    try:
        resp = requests.post(SEATALK_EMP_API,
            json={"emails": SEATALK_USERS},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            timeout=15)
        data = resp.json()
        if data.get("code") == 0:
            cache = {}
            codes = []
            for emp in data.get("employees", []):
                email = emp.get("email", "")
                code = emp.get("employee_code", "")
                if emp.get("code") == 0 and code:
                    cache[email] = code
                    codes.append(code)
                    print(f"[Emp] {email} → employee_code: {code}")
                else:
                    print(f"[Emp] {email} → 未找到, code={emp.get('code')}")
            # 保存缓存
            with open(SEATALK_EMP_CACHE, "w") as f:
                json.dump(cache, f)
            return codes
    except Exception as e:
        print(f"[Emp] 查询失败: {e}")

    return SEATALK_USERS  # fallback


def send_group_evidence(first_resp, evidence, headers):
    """把推导过程作为线程回复发到群消息下；线程回复失败则退化为普通跟进消息"""
    mid = first_resp.get("message_id") or (first_resp.get("data") or {}).get("message_id")
    if mid:
        # thread_id 必须放在 message 对象内部（已实测验证：顶层位置会被忽略）
        try:
            body = {"group_id": SEATALK_GROUP_ID,
                    "message": {"tag": "text", "text": {"format": 1, "content": evidence},
                                "thread_id": mid}}
            r2 = requests.post(SEATALK_GROUP_API, json=body, headers=headers, timeout=15)
            d2 = r2.json()
            if d2.get("code") == 0:
                print(f"[Seatalk] ✅ 推导过程已回复到线程 (msg_id={mid})")
                return
        except:
            pass
        print("[Seatalk] ⚠️ 线程回复失败，退化为普通跟进消息")
    # 退化：普通跟进消息，前缀标注
    fallback = {
        "group_id": SEATALK_GROUP_ID,
        "message": {"tag": "text", "text": {"format": 1,
                    "content": "↪️ 回复上条抄底信号的分析依据：\n\n" + evidence}},
    }
    try:
        resp = requests.post(SEATALK_GROUP_API, json=fallback, headers=headers, timeout=15)
        if resp.status_code == 200 and resp.json().get("code") == 0:
            print("[Seatalk] ✅ 推导过程已作为跟进消息发送")
    except Exception as e:
        print(f"[Seatalk] ⚠️ 跟进消息发送失败: {e}")


def send_seatalk(message, mode="daily", evidence=None):
    """发送消息。test 模式仅通知主用户，其余通知全组。
    evidence: 抄底信号的推导过程文本，发送后作为线程回复附在信号消息下"""
    if not SEATALK_APP_ID or not SEATALK_APP_SECRET:
        print("\n⚠️  未配置 SEATALK_APP_ID / SEATALK_APP_SECRET")
        print("=" * 50)
        print(message)
        if evidence:
            print(evidence)
        print("=" * 50)
        return False

    token = get_access_token()
    if not token:
        print("\n⚠️  无法获取 access token")
        print("=" * 50)
        print(message)
        if evidence:
            print(evidence)
        print("=" * 50)
        return False

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # 群聊模式：非 test 模式且设置了 GROUP_ID 时发群聊
    if SEATALK_GROUP_ID and mode != "test":
        body = {
            "group_id": SEATALK_GROUP_ID,
            "message": {
                "tag": "text",
                "text": {"format": 1, "content": message},
            }
        }
        resp = requests.post(SEATALK_GROUP_API, json=body, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                print(f"[Seatalk] ✅ 已发送到群 {SEATALK_GROUP_ID}")
                if evidence:
                    send_group_evidence(data, evidence, headers)
                return True
            print(f"[Seatalk] ❌ 群发失败: {data.get('message', '')}")
        return False

    # 私聊模式：发给每个用户
    all_codes = get_employee_codes()
    emp_codes = all_codes[:1] if mode == "test" else all_codes

    def send_one(emp_code):
        body = {
            "employee_code": emp_code,
            "message": {
                "tag": "markdown",
                "markdown": {"content": message},
            }
        }
        resp = requests.post(SEATALK_SINGLE_API, json=body, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("code") == 0, data
        return False, {"code": -1, "message": f"HTTP {resp.status_code}"}

    success_count = 0
    for emp_code in emp_codes:
        ok, data = send_one(emp_code)
        if ok:
            print(f"[Seatalk] ✅ 已发送到 {emp_code}")
            success_count += 1
        elif data.get("code") in (100, 3001):
            print(f"[Seatalk] 清除缓存重试...")
            try:
                os.remove(SEATALK_TOKEN_FILE)
            except:
                pass
            try:
                os.remove(SEATALK_EMP_CACHE)
            except:
                pass
            token = get_access_token()
            emp_codes = get_employee_codes()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                ok2, _ = send_one(emp_code)
                if ok2:
                    print(f"[Seatalk] ✅ 重试成功 {emp_code}")
                    success_count += 1
                    continue
            print(f"[Seatalk] ❌ 重试失败 {emp_code}")
        else:
            print(f"[Seatalk] ❌ 发送失败 {emp_code}: {data.get('message', '')}")

    # 私聊模式下推导过程作为跟进消息发给每位收件人
    if evidence and success_count > 0:
        for emp_code in emp_codes:
            try:
                body = {"employee_code": emp_code,
                        "message": {"tag": "markdown",
                                    "markdown": {"content": "↪️ 推导过程：\n\n" + evidence}}}
                requests.post(SEATALK_SINGLE_API, json=body, headers=headers, timeout=15)
            except:
                pass
        print(f"[Seatalk] ✅ 推导过程已发送到 {success_count} 位收件人")

    if success_count > 0:
        print(f"[Seatalk] 发送完成: {success_count}/{len(emp_codes)} 成功")
        return True
    else:
        print("\n⚠️  全部发送失败，消息内容:")
        print("=" * 50)
        print(message)
        print("=" * 50)
        return False


def settle_pending_trade():
    """结算上次的待定交易，自动更新历史数据库"""
    try:
        with open(PENDING_TRADE_FILE) as f:
            pending = json.load(f)
    except:
        return None  # 无待定交易

    # 今天的交易还没完成，不能结算（要到明天开盘才知道结果）
    if pending.get("date") == datetime.now().strftime("%Y-%m-%d"):
        return None

    price = pending.get("entry_price", 0)
    if price <= 0:
        return None

    exit_price = None
    data = fetch_realtime()
    if data:
        exit_price = data.get("open")  # 次日开盘价卖出

    if not exit_price or exit_price <= 0:
        return None  # 还没到出场时间

    # 计算实际收益
    ret = (exit_price - price) / price * 100
    result = "win" if ret > 0 else "loss"

    # 构建交易记录
    trade = {
        "date": pending.get("date", ""),
        "delta": pending.get("delta", 0),
        "premium": pending.get("premium", 0),
        "ret": round(ret, 2),
        "vol_ratio": pending.get("vol_ratio", 1.0),
        "trend_5d": pending.get("trend_5d", 0),
        "result": result,
        "type": "real",  # 标记为实盘交易
    }

    # 追加到历史数据库（按日期去重，避免重复记录）
    trades = load_historical_trades()
    # 移除同日同类型记录
    trades = [t for t in trades if not (t.get("date") == trade["date"] and t.get("type") == trade.get("type"))]
    trades.append(trade)

    with open(HISTORICAL_TRADES_FILE, "w") as f:
        json.dump(trades, f, ensure_ascii=False)

    # 清除待定
    os.remove(PENDING_TRADE_FILE)

    emoji = "🟢" if result == "win" else "🔴"
    print(f"[Trade] 结算: {pending['date']} 买入{price:.3f} → 卖出{exit_price:.3f} = {emoji} {ret:+.2f}%")
    print(f"[Trade] 历史数据库已更新: {len(trades)}笔 (含实盘)")

    return trade


def save_pending_trade(analysis):
    """信号触发时保存待定交易（同一天内后来的覆盖前面的，更接近收盘价）"""
    trade = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "entry_price": analysis["price"],
        "delta": analysis.get("delta", 0),
        "premium": analysis.get("iopv_premium") or analysis.get("nav_premium") or 0,
        "vol_ratio": 1.0,
        "trend_5d": analysis.get("change_pct", 0),
    }

    # 检查是否覆盖旧记录
    try:
        with open(PENDING_TRADE_FILE) as f:
            old = json.load(f)
            if old.get("date") == trade["date"]:
                print(f"[Trade] 更新待定交易: {old['entry_price']:.3f} → {trade['entry_price']:.3f}（更接近收盘）")
    except:
        pass

    with open(PENDING_TRADE_FILE, "w") as f:
        json.dump(trade, f)
    print(f"[Trade] 待定交易已保存: {trade['date']} @ {trade['entry_price']:.3f}")


def sync_to_github():
    """本地数据变更后自动 push 到 GitHub（后台，不阻塞）"""
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        # 只推送数据文件
        subprocess.run(
            ["git", "-C", script_dir, "add", "historical_trades_159509.json", "pending_trade_159509.json", "run.log"],
            capture_output=True, timeout=10)
        result = subprocess.run(
            ["git", "-C", script_dir, "diff", "--cached", "--quiet"],
            capture_output=True, timeout=10)
        if result.returncode != 0:
            subprocess.run(
                ["git", "-C", script_dir, "commit", "-m", "data: local sync"],
                capture_output=True, timeout=10)
            subprocess.run(
                ["git", "-C", script_dir, "push", "origin", "main"],
                capture_output=True, timeout=30)
            print("[Sync] ✅ 已推送到 GitHub")
        else:
            print("[Sync] 无变更")
    except Exception as e:
        print(f"[Sync] 推送失败(非致命): {e}")


def main():
    now = datetime.now()

    if now.weekday() >= 5 and "--test" not in sys.argv:
        print("Weekend - skipping")
        return

    mode = "daily"
    if "--intraday" in sys.argv:
        mode = "intraday"
    elif "--test" in sys.argv:
        mode = "test"
    elif "--daily" in sys.argv:
        mode = "daily"

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] ETF Monitor | mode={mode} | cost={COST_MODEL}")

    # 先结算上次的待定交易
    settled = settle_pending_trade()

    # 结算后再分析（确保胜率用最新数据）
    analysis = analyze()

    # 信号触发时，保存待定交易
    if analysis.get("signal") in ("strong_buy", "buy", "weak_buy"):
        save_pending_trade(analysis)

    # 盘后模式：如果结算了交易，把结果加到消息里
    if settled:
        analysis["settled_trade"] = settled

    # 数据质量标签
    data_quality = []
    if not analysis.get("iopv"):
        data_quality.append("⚠️ IOPV缺失(yf fallback)")
    if not analysis.get("nav"):
        data_quality.append("⚠️ NAV暂未公布（19:00尚早）")
    else:
        nav_date = analysis.get("nav_date", "")
        if nav_date:
            from datetime import date as dt_date
            try:
                nav_d = dt_date.fromisoformat(nav_date)
                lag = (dt_date.today() - nav_d).days
                if lag > 2:
                    data_quality.append(f"⚠️ NAV滞后{lag}天（今日NAV可能未出）")
            except:
                pass
    analysis["data_quality"] = " | ".join(data_quality) if data_quality else "✅ 数据正常"

    # AI 分析：每次都跑
    if AI_ENABLED:
        print(f"[AI] 调用 DeepSeek 分析 (mode={mode})...")
        ai_result = ai_analyze(analysis, mode)
        if ai_result:
            analysis["ai_analysis"] = ai_result
            print(f"[AI] ✅ 分析完成")

    # 写诊断日志（AI 之后，确保日志记录真实状态）
    write_diag_log(analysis, mode, analysis.get("ai_analysis"))

    message = format_message(analysis)

    # 盘中模式每天都发，不跳过。无信号时标注"观望"

    if mode == "test":
        print("\n" + "=" * 50)
        print("📡 数据源检查")
        print(f"  行情: {'✅' if 'price' in analysis else '❌'} price={analysis.get('price', 'N/A')}")
        print(f"  IOPV: {analysis.get('iopv', 'N/A')}")
        print(f"  NAV: {'✅' if 'nav' in analysis else '❌'} {analysis.get('nav', 'N/A')}")
        print(f"  溢价: {analysis.get('iopv_premium', 'N/A')}%")
        print(f"  Δ溢价: {analysis.get('delta', 'N/A')}%")
        cost = analysis.get("cost", {})
        print(f"  成本: {cost.get('total_pct', 'N/A')}%/笔 ≈ ¥{cost.get('total_yuan', 'N/A')}")
        print(f"  信号: {analysis.get('signal_text', 'N/A')}")
        has_id = bool(SEATALK_APP_ID)
        has_secret = bool(SEATALK_APP_SECRET)
        print(f"  Seatalk 收件人: {', '.join(SEATALK_USERS)}")
        print(f"  App ID: {'✅' if has_id else '⚠️ 未设置'}")
        print(f"  App Secret: {'✅' if has_secret else '⚠️ 未设置'}")
        if has_id and has_secret:
            token = get_access_token()
            print(f"  Token: {'✅' if token else '❌ 获取失败'}")
        print(f"  AI 分析: {'✅ 已启用' if AI_ENABLED else '⚪ 未启用（设置 DEEPSEEK_API_KEY）'}")
        # 历史数据库
        trades = load_historical_trades()
        real_trades = [t for t in trades if t.get("type") == "real"]
        print(f"  历史数据库: {len(trades)}笔（回测{len(trades)-len(real_trades)}+实盘{len(real_trades)}，自动更新）")
        if os.path.exists(PENDING_TRADE_FILE):
            with open(PENDING_TRADE_FILE) as f:
                p = json.load(f)
            print(f"  ⏳ 待定交易: {p['date']} @ {p['entry_price']:.3f}")
        if analysis.get("est_net"):
            net_yuan = analysis['est_net'] * cost.get('capital', 20000) / 100
            print(f"  预估净收益: {analysis['est_net']:+.2f}%/笔 ≈ ¥{net_yuan:+.2f}")
        if analysis.get("ai_analysis"):
            print(f"  AI 分析结果: {analysis['ai_analysis'][:100]}...")

    # 抄底信号：生成推导过程，随消息线程回复佐证
    evidence = None
    if analysis.get("signal") in ("strong_buy", "buy", "weak_buy"):
        evidence = build_evidence(analysis)

    send_seatalk(message, mode, evidence)

    # 自动同步到 GitHub（有变更才 push）
    if mode != "test":
        sync_to_github()

    print("Done.")


if __name__ == "__main__":
    main()
