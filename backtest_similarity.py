#!/usr/bin/env python3
"""相似度算法回测验证（无未来函数）

对每笔历史交易，只用它**之前**的交易做相似日匹配，
看"最相似3日中多数赢"能否预测该日真实输赢，
对比旧算法（abs+余弦）、新算法（z-score+欧氏距离）与基线（先例池多数类）。

用法: python3 backtest_similarity.py
"""
import json
import math
from math import comb

def old_algo(current, pool, top_n=3):
    """旧算法: abs() + 余弦相似度（只比较方向，对大小不敏感）"""
    features = ['delta', 'premium', 'trend_5d']
    def vec(t): return [abs(t.get(f, 0) or 0) for f in features]
    cv = vec(current)
    nc = sum(x * x for x in cv) ** 0.5
    if nc == 0:
        return []
    out = []
    for t in pool:
        tv = vec(t)
        if any(math.isnan(v) for v in tv):
            continue
        nt = sum(x * x for x in tv) ** 0.5
        if nt == 0:
            continue
        sim = sum(a * b for a, b in zip(cv, tv)) / (nc * nt)
        if math.isnan(sim):
            continue
        out.append((round(sim * 100), t))
    out.sort(key=lambda x: -x[0])
    return out[:top_n]

def new_algo(current, pool, top_n=3):
    """新算法: z-score 标准化 + 欧氏距离（保留符号，体现量级）"""
    features = ['delta', 'premium', 'trend_5d']
    def raw(t): return [t.get(f, 0) or 0 for f in features]
    rows = [t for t in pool if all(not math.isnan(v) for v in raw(t))]
    if not rows:
        return []
    stats = {}
    for i, f in enumerate(features):
        vals = [raw(t)[i] for t in rows]
        m = sum(vals) / len(vals)
        s = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
        stats[f] = (m, s if s > 0 else 1.0)
    def zv(vals):
        return [(v - stats[f][0]) / stats[f][1] for f, v in zip(features, vals)]
    cz = zv(raw(current))
    out = []
    for t in rows:
        tz = zv(raw(t))
        d = sum((a - b) ** 2 for a, b in zip(cz, tz)) ** 0.5
        out.append((round(100 / (1 + d)), t))
    out.sort(key=lambda x: -x[0])
    return out[:top_n]

def binom_tail(n, k):
    """P(Bin(n, 0.5) >= k) 精确计算"""
    return sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n

def backtest(path, label):
    trades = json.load(open(path))
    trades.sort(key=lambda t: t['date'])
    print(f"== {label}: {len(trades)} 笔 ==")
    res = {name: {'pred': 0, 'hit': 0}
           for name in ('base', 'old', 'new', 'top1_old', 'top1_new')}
    b = c = 0  # McNemar 不一致对: b=旧对新错, c=旧错新对
    for i, t in enumerate(trades):
        if i < 3:
            continue  # 至少3个先例才能取 top3
        pool = trades[:i]  # 只看它之前的交易，无未来函数
        cur = {'delta': t.get('delta', 0),
               'premium': t.get('premium', 0),
               'trend_5d': t.get('trend_5d', 0)}
        actual = 1 if t.get('result') == 'win' else 0
        # 基线: 先例池的多数类
        pool_wins = sum(1 for p in pool if p.get('result') == 'win')
        base_pred = 1 if pool_wins * 2 >= len(pool) else 0
        res['base']['pred'] += 1
        res['base']['hit'] += (1 if base_pred == actual else 0)
        preds = {}
        for name, algo in (('old', old_algo), ('new', new_algo)):
            top = algo(cur, pool)
            if len(top) < 3:
                preds[name] = None
                continue
            wins = sum(1 for _, d in top if d.get('result') == 'win')
            pred = 1 if wins >= 2 else 0
            preds[name] = pred
            res[name]['pred'] += 1
            res[name]['hit'] += (1 if pred == actual else 0)
            top1 = 1 if top[0][1].get('result') == 'win' else 0
            res['top1_' + name]['pred'] += 1
            res['top1_' + name]['hit'] += (1 if top1 == actual else 0)
        if preds['old'] is not None and preds['new'] is not None:
            if preds['old'] == actual and preds['new'] != actual:
                b += 1
            if preds['old'] != actual and preds['new'] == actual:
                c += 1
    def pct(k):
        return f"{res[k]['hit']}/{res[k]['pred']} = {res[k]['hit'] / max(res[k]['pred'], 1) * 100:.1f}%"
    print(f"  基线(先例池多数类)   : {pct('base')}")
    print(f"  旧算法 top3 多数投票 : {pct('old')}")
    print(f"  新算法 top3 多数投票 : {pct('new')}")
    print(f"  旧算法 top1          : {pct('top1_old')}")
    print(f"  新算法 top1          : {pct('top1_new')}")
    n = b + c
    p = 2 * binom_tail(n, max(b, c)) if n > 0 else 1.0
    print(f"  McNemar 配对检验: 不一致对 {n} 天 (旧对新错 {b}, 旧错新对 {c}), 精确 p = {p:.4f}")
    print()

if __name__ == '__main__':
    backtest('historical_trades.json', '159687 亚太精选')
    backtest('historical_trades_159509.json', '159509 纳指科技')
