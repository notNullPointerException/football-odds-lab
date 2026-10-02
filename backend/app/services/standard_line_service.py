# -*- coding: utf-8 -*-
"""标准线 —— 用库里的赛果标定一条参考线。

【为什么要有这个】
之前想拿 Pinnacle 当基准算 EV（见 odds_watch_service），查穿三个库后确认：
**没有历史价格**。`odds_snapshots` 36 行全是同一天手测的残渣、`fixture_id` 全 NULL；
`football_engine.bet_journal` 只有 8 天且 113 条让球腿的 `fair_p` 一条都没有。
价格过期不补（锐盘 ~5 天清除），EV 只能靠攒。

但**赛果早就在库里了**：`fixtures` 10 万场已完赛，横跨 2022-01 → 今天。
所以先用赛果标定一条参考线 —— 给定任意盘口线，直接读出真实三路概率。
有了它，竞彩赔率一到手就能算 EV，**不再依赖任何博彩公司**。

【核心：一张经验查表，不是泊松公式】
    读表键：μ = λh - λa（期望净胜球，只用赛果算）
    读数：  P(净胜球 = k | μ 落在桶内)      k ∈ [-8, +8]，桶宽 0.25

为什么不直接用泊松 —— 实测（78,130 场）μ 是**过度离散**的：
    μ ∈ [+1.50, +9) 的桶，实际均净胜只有 +1.23
    μ ∈ [-9.00, -1.50) 的桶，实际均净胜只有 -0.74
预测的差距比实现的差距大一截。泊松公式照 μ 直接算会系统性算歪，
而经验表天然带着真实的形状（肥尾、真实平局率）。这正是本模块相对
`backtest_heads.margin_dist` / `jc_combos.lam_hcap` 那套泊松网格的价值。

有了 {k: p} 这张表，任意市场都是求和：
    胜平负:  主胜 = Σ_{k>0} p   平 = p[0]   客胜 = Σ_{k<0} p
    让球 g:  让胜 = Σ_{k+g>0} p  让平 = p[-g]  让负 = Σ_{k+g<0} p
                ↑ g = 竞彩 goalLine（负=主队让球）
    （用严格不等号写，所以整数线和半球线都对；三个概率相加恒等于 1）

⚠️ **绝不碰 standings。** 它是一张每天被 upsert 覆盖的**当前快照**，
不是「当时」的台账。`prediction/features.py:445` 把它 merge 进每一个历史
赛程，等于把整季最终排名泄进历史行。本模块只读 `fixtures` 的比分。

⚠️ **causal 是命根子。** 每一场的强度只用它**之前**的赛果（`date < kickoff`）。
   验证方法：`compute_series(..., leak=True)` 重跑，指标必须变差；
   不变就是有隐蔽泄漏，先查再往下走。

【只写自己的表，不碰任何模型/闸门】
本模块只做参考与展示。闸门①-⑤、三个模型头、任何阈值一行不动。
"""
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import text

from app.db.session import SessionLocal

MARKET_1X2 = "1x2"
MARKET_HHAD = "hhad"

SEL_1X2 = ("主胜", "平", "客胜")
SEL_HHAD = ("让胜", "让平", "让负")

# 净胜球存储范围。经验分布要归一化，|k| > 8 的极端比分极其罕见，尾部并进边缘格。
KM_HI = 8
KM_LO = -8

LAM_FLOOR = 0.05      # λ 下限，防止空窗期球队算出 0 导致 μ 病态

# 默认参数。改这些必须重建（build_id 会不同，旧批不删，可对比）。
DEF_WINDOW = 10       # 滚动窗口场次
DEF_MIN_PRIOR = 4     # 冷启动：两队各至少打过几场
DEF_BW = 0.25         # μ 桶宽
DEF_LO, DEF_HI = -3.0, 3.0   # μ 桶覆盖范围（两端外各一个开口桶）

_DDL = [
    """CREATE TABLE IF NOT EXISTS std_line_meta (
        build_id VARCHAR(32) PRIMARY KEY COMMENT '构建标识,换参数重跑会新增一批',
        built_at DATETIME NOT NULL COMMENT '构建时间',
        window_matches INT NOT NULL COMMENT '滚动窗口场次',
        min_prior INT NOT NULL COMMENT '冷启动最少历史场次',
        bucket_width DECIMAL(4,2) NOT NULL COMMENT 'mu 桶宽',
        src_from DATE NOT NULL COMMENT '源数据起',
        src_to DATE NOT NULL COMMENT '源数据止',
        n_total INT NOT NULL COMMENT '读到的已完赛场次',
        n_used INT NOT NULL COMMENT '两支球队历史都够、真正进表的场次',
        params VARCHAR(255) NULL COMMENT '其它参数/备注',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS std_line_margin (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        build_id VARCHAR(32) NOT NULL COMMENT '关联 std_line_meta.build_id',
        bucket_ix INT NOT NULL COMMENT 'mu 桶序号,由 mu 唯一决定(查表用这个,避开浮点比较)',
        bucket_lo DECIMAL(5,2) NULL COMMENT 'mu 桶下界(含);NULL=开口(-inf)',
        bucket_hi DECIMAL(5,2) NULL COMMENT 'mu 桶上界(不含);NULL=开口(+inf)',
        margin_k INT NOT NULL COMMENT '净胜球 k,范围 -8..8',
        p DECIMAL(8,6) NOT NULL COMMENT 'P(净胜球=k | mu 在本桶),桶内已归一化',
        n INT NOT NULL COMMENT '本桶样本场次(同桶每行相同)',
        UNIQUE KEY uk_row (build_id, bucket_ix, margin_k),
        KEY idx_build (build_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def ensure_tables():
    db = SessionLocal()
    try:
        for ddl in _DDL:
            db.execute(text(ddl))
        db.commit()
    finally:
        db.close()


# ──── μ 的算法：只吃赛果的滚动强度 ────

def _lambdas(ah, dh, aa, da, lg, mean_h, mean_a):
    """乘性模型：主队攻 x 客队防，再乘主客场因子。

    ah/dh = 主队滚动场均进/失，aa/da = 客队滚动场均进/失，
    lg = 截至当日的场均每队进球（联赛基准），mean_h/mean_a = 主/客场均。

    两队都平庸时（ah≈da≈aa≈dh≈lg）应还原成 λh≈mean_h、λa≈mean_a，所以
    主场因子写成 mean_h/lg（而不是硬编码 1.10）。主客场因子之和恒为 2。
    """
    lh = ah * da / lg * (mean_h / lg)
    la = aa * dh / lg * (mean_a / lg)
    return max(LAM_FLOOR, lh), max(LAM_FLOOR, la)


def compute_series(rows, window=DEF_WINDOW, min_prior=DEF_MIN_PRIOR, leak=False):
    """一趟扫完所有赛果，产出每场的 (date, lambda_home, lambda_away, margin)。

    返回 λ 对而不只是 μ=λh-λa，是为了让泊松基准（backtest_heads.margin_dist）
    能在【完全相同的样本】上做对照 —— 否则「经验表 vs 泊松表」就不公平。

    🔴 causal：第 i 场的 μ 只用第 0..i-1 场的赛果。`leak=True` 是【故意留的
    漏气开关】，把当前这场先记进历史再算 μ —— 只用来做「无泄漏自证」。
    打开它指标必须变差；变不差就说明别处还有隐蔽泄漏，先查清楚。

    rows 需按 date 升序，字段：date/home_id/away_id/goals_home/goals_away。
    """
    scar = defaultdict(lambda: deque(maxlen=window))
    scon = defaultdict(lambda: deque(maxlen=window))
    tot_h = tot_a = tot_n = 0          # 截至当日的累计进球，用于算联赛基准
    out = []

    for r in rows:
        h, a = r["home_id"], r["away_id"]
        gh, ga = int(r["goals_home"]), int(r["goals_away"])

        if leak:      # ⚠️ 只有自证时才为真。这一行就是把答案先塞进历史。
            scar[h].append(gh); scon[h].append(ga)
            scar[a].append(ga); scon[a].append(gh)
            tot_h += gh; tot_a += ga; tot_n += 1

        if tot_n >= 200 and len(scar[h]) >= min_prior and len(scar[a]) >= min_prior:
            mean_h = tot_h / tot_n
            mean_a = tot_a / tot_n
            lg = (mean_h + mean_a) / 2.0
            if lg > 0:
                ah = sum(scar[h]) / len(scar[h])
                dh = sum(scon[h]) / len(scon[h])
                aa = sum(scar[a]) / len(scar[a])
                da = sum(scon[a]) / len(scon[a])
                lh, la = _lambdas(ah, dh, aa, da, lg, mean_h, mean_a)
                out.append((r["date"], lh, la, gh - ga))

        if not leak:  # 正常路径：记完这一场才计入历史。顺序颠倒就是漏气。
            scar[h].append(gh); scon[h].append(ga)
            scar[a].append(ga); scon[a].append(gh)
            tot_h += gh; tot_a += ga; tot_n += 1

    return out


def bucket_ix(mu, bw=DEF_BW, lo=DEF_LO, hi=DEF_HI):
    """μ -> 桶序号。开口桶：两端各吞掉所有越界的 μ。

    构建期和查表期【必须走同一个函数】，否则刻度对不上。
    """
    n = int(round((hi - lo) / bw))
    if mu <= lo:
        return 0
    if mu >= hi:
        return n
    return int((mu - lo) // bw) + 1


def bucket_bounds(ix, bw=DEF_BW, lo=DEF_LO, hi=DEF_HI):
    """桶序号 -> (下界, 上界)。开口端返回 None（写库时落 NULL）。"""
    n = int(round((hi - lo) / bw))
    if ix <= 0:
        return None, lo
    if ix >= n:
        return hi, None
    return lo + (ix - 1) * bw, lo + ix * bw


# ──── 构建 ────

def _load_fixtures(db):
    return list(db.execute(text("""
        SELECT date, home_id, away_id, goals_home, goals_away
        FROM fixtures
        WHERE goals_home IS NOT NULL AND goals_away IS NOT NULL
          AND status_short IN ('FT','AET','PEN')
        ORDER BY date
    """)).mappings())


def build(window=DEF_WINDOW, min_prior=DEF_MIN_PRIOR, bw=DEF_BW,
          build_id=None, leak=False, db=None):
    """从赛果重建标准线。旧批不删 —— 对比多批就能看出线随数据积累怎么动。"""
    own = db is None
    db = db or SessionLocal()
    try:
        rows = _load_fixtures(db)
        if not rows:
            raise RuntimeError("fixtures 里没有已完赛数据，标准线无从谈起")
        ser = compute_series(rows, window=window, min_prior=min_prior, leak=leak)
        if not ser:
            raise RuntimeError("一场都没算出来，检查 min_prior / 数据覆盖")

        cells = defaultdict(lambda: defaultdict(int))
        for _dt, lh, la, m in ser:
            cells[bucket_ix(lh - la, bw)][min(KM_HI, max(KM_LO, m))] += 1

        bid = build_id or datetime.now().strftime("b%Y%m%d-%H%M%S")
        out = []
        for ix, dist in cells.items():
            lo_b, hi_b = bucket_bounds(ix, bw)
            n = sum(dist.values())
            for k in range(KM_LO, KM_HI + 1):
                out.append({"bid": bid, "ix": ix, "lo": lo_b, "hi": hi_b,
                            "k": k, "p": dist.get(k, 0) / n, "n": n})

        # 同 build_id 重跑 = 覆盖（幂等），不翻倍
        db.execute(text("DELETE FROM std_line_meta WHERE build_id=:bid"), {"bid": bid})
        db.execute(text("DELETE FROM std_line_margin WHERE build_id=:bid"), {"bid": bid})
        db.execute(text("""
            INSERT INTO std_line_meta
                (build_id, built_at, window_matches, min_prior, bucket_width,
                 src_from, src_to, n_total, n_used, params)
            VALUES (:bid, :now, :w, :mp, :bw, :f, :t, :nt, :nu, :pa)
        """), {
            "bid": bid, "now": datetime.now(), "w": window, "mp": min_prior, "bw": bw,
            "f": rows[0]["date"].date(), "t": rows[-1]["date"].date(),
            "nt": len(rows), "nu": len(ser),
            "pa": f"KM={KM_LO}..{KM_HI} 严格不等号分类" + (" LEAK" if leak else ""),
        })
        db.execute(text("""
            INSERT INTO std_line_margin
                (build_id, bucket_ix, bucket_lo, bucket_hi, margin_k, p, n)
            VALUES (:bid, :ix, :lo, :hi, :k, :p, :n)
        """), out)
        db.commit()
        return {
            "build_id": bid, "n_total": len(rows), "n_used": len(ser),
            "buckets": len(cells), "rows": len(out),
            "src_from": rows[0]["date"].date(), "src_to": rows[-1]["date"].date(),
        }
    finally:
        if own:
            db.close()


def latest_build(db=None):
    own = db is None
    db = db or SessionLocal()
    try:
        r = db.execute(text("""
            SELECT build_id, built_at, n_total, n_used, src_from, src_to, window_matches
            FROM std_line_meta ORDER BY built_at DESC LIMIT 1
        """)).mappings().first()
        return dict(r) if r else None
    finally:
        if own:
            db.close()


# ──── 查表 ────

def bucket_dist(build_id, mu, db=None, bw=DEF_BW):
    """取 μ 所在桶的经验净胜球分布 {k: p}。与 backtest_heads.margin_dist 同形。"""
    own = db is None
    db = db or SessionLocal()
    try:
        rs = db.execute(text("""
            SELECT margin_k, p FROM std_line_margin
            WHERE build_id=:b AND bucket_ix=:ix
        """), {"b": build_id, "ix": bucket_ix(mu, bw)}).mappings().all()
        return {r["margin_k"]: float(r["p"]) for r in rs}
    finally:
        if own:
            db.close()


def fair(build_id, mu, market, line=None, db=None):
    """μ + 玩法 -> 三路真实概率。market='1x2' / 'hhad'（hhad 需给 line，负=主队让球）。

    用严格不等号分类（k+g>0 / ==0 / <0），所以整数线和半球线都对，
    且三路相加恒等于 1 —— 这不是巧合，是分类本身完备。
    """
    dist = bucket_dist(build_id, mu, db=db)
    if not dist:
        raise LookupError(f"build_id={build_id} 的桶 ix={bucket_ix(mu)} 没有数据")
    if market == MARKET_1X2:
        return {
            "主胜": sum(p for k, p in dist.items() if k > 0),
            "平": dist.get(0, 0.0),
            "客胜": sum(p for k, p in dist.items() if k < 0),
        }
    if market == MARKET_HHAD:
        if line is None:
            raise ValueError("hhad 必须给 line（竞彩 goalLine，负=主队让球）")
        g = float(line)
        return {
            "让胜": sum(p for k, p in dist.items() if k + g > 0),
            "让平": sum(p for k, p in dist.items() if k + g == 0),
            "让负": sum(p for k, p in dist.items() if k + g < 0),
        }
    raise ValueError(f"未知玩法 {market!r}，只认 {MARKET_1X2}/{MARKET_HHAD}")


# ──── 推断期：给一场还没踢的比赛算 μ ────
# ⚠️ 必须和构建期同刻度：两边都走 _lambdas()，联赛基准都按「截至 kickoff 的全部赛果」算。

def _team_form_before(db, team_id, before, window=DEF_WINDOW):
    """该队 before 之前最近 window 场的场均进/失（主客不分，与构建期一致）。"""
    r = db.execute(text("""
        SELECT AVG(gs) AS gf, AVG(gc) AS ga, COUNT(*) AS n FROM (
            SELECT u.d, u.gs, u.gc FROM (
                SELECT date AS d, goals_home AS gs, goals_away AS gc FROM fixtures
                 WHERE home_id=:t AND date < :dt AND goals_home IS NOT NULL
                UNION ALL
                SELECT date AS d, goals_away AS gs, goals_home AS gc FROM fixtures
                 WHERE away_id=:t AND date < :dt AND goals_away IS NOT NULL
            ) u ORDER BY u.d DESC LIMIT :w
        ) x
    """), {"t": team_id, "dt": before, "w": window}).mappings().first()
    return float(r["gf"] or 0), float(r["ga"] or 0), int(r["n"] or 0)


def expected_margin(db, home_id, away_id, kickoff, window=DEF_WINDOW, min_prior=DEF_MIN_PRIOR):
    """给一场还没踢的比赛算 μ。历史不够返回 None（与构建期的 min_prior 一致）。"""
    ah, dh, nh = _team_form_before(db, home_id, kickoff, window)
    aa, da, na = _team_form_before(db, away_id, kickoff, window)
    if nh < min_prior or na < min_prior:
        return None
    lg_r = db.execute(text("""
        SELECT AVG(goals_home) mh, AVG(goals_away) ma, COUNT(*) n
        FROM fixtures
        WHERE date < :dt AND goals_home IS NOT NULL AND goals_away IS NOT NULL
    """), {"dt": kickoff}).mappings().first()
    if not lg_r or not lg_r["n"]:
        return None
    mean_h, mean_a = float(lg_r["mh"]), float(lg_r["ma"])
    lh, la = _lambdas(ah, dh, aa, da, (mean_h + mean_a) / 2.0, mean_h, mean_a)
    return lh - la
