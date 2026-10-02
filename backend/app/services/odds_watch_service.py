# -*- coding: utf-8 -*-
"""竞彩 vs 锐盘（Pinnacle）赔率快照 —— 换一个「水」的来源。

【为什么要有这个】
选腿的依据一直是「模型比市场准」。633 场全历史回测把这个依据推翻了：
prob 头劣于频率基准、λ 头 ≈ 零 skill、LLM 47% —— **edge 不在预测层**。

所以换一个来源：不靠模型，靠「竞彩自己定价偏」。要验证它，需要一个比竞彩准的
基准 —— 用 Pinnacle（全球公认最锐）。本模块每天抓一次两边的价，落库攒样本，
事后回答「竞彩赔率 > 锐盘公平价」的那些腿 ROI 到底多少。

【核心：Pinnacle 的盘口能精确还原竞彩的整数让球线】
Pinnacle 的亚洲让球盘开的是 ±1.5/±2.5/±3.5（半一、球半），竞彩开的是整数线
-1/-2。看着对不上 —— 而让球盘恰恰是唯一「有水」的地方。但这一族正好够还原。

令 g = 竞彩 goalLine（负=主队让球），m = 主队净胜球，a = m + g（竞彩调整后净胜球）：
    让胜  a >= 1   <=>  m >= 1-g
    让平  a == 0   <=>  m == -g
    让负  a <= -1  <=>  m <= -1-g

    让胜 = P(m >= 1-g)
    让负 = 1 - P(m >= -g)
    让平 = P(m >= -g) - P(m >= 1-g)      <- 两个相邻档相减

其中 P(m >= k) 从 Pinnacle 取（这就是为什么 ±1.5/±2.5/±3.5 这一族够用）：
    k == 1   -> P(MW Home)                （不需要额外盘口）
    k == 0   -> 1 - P(MW Away)            （同上）
    其他     -> P(AH "Home -(k-0.5)")     （k=2 -> -1.5；k=-1 -> +1.5）

竞彩实际开的 ±1/±2/±3 线全部覆盖，不需要任何 Pinnacle 没开的盘口。
按上式三个概率相加恒等于 1（构造保证）。

⚠️ **已知近似**：MW 与 AH 是两个独立市场，各自去水后再相减，严格说跨市场相减
不完全严谨（两边各有自己的水位）。这是行业惯用做法，误差量级 ~2-3% 水位。
`pin_note` 列记录了每一档用的是哪条腿，事后可复核。

【和别的模块的关系】
- 竞彩侧**零新代码**：直接调 `app.api.jc._build_payload`（就是前端那个端点用的
  同一个函数），去水概率、让球线、fixture_id 关联全都现成。
- 锐盘侧走 `app.core.apifootball.api_get`（自带退避重试 + 「HTTP200 假成功」检测）。
- **只写自己的表，不碰任何模型/闸门**。本模块只做标注，不改选腿规则。
"""
from datetime import datetime, timedelta

from sqlalchemy import text
from loguru import logger

from app.api.jc import _build_payload
from app.core.apifootball import api_get, check_api_error
from app.db.session import SessionLocal

# Pinnacle 在 API-Football 的 bookmaker id（实测确认，查法 GET /odds/bookmakers）：
#   id=3 是 **Betfair**，id=4 才是 **Pinnacle**。
#   🔴 曾把这里写成 3 —— 结果整轮分析都在拿 Betfair 当「全球最锐」，报出去的表
#   书名全标错了。改 id 前先查 /odds/bookmakers，别凭印象。
PINNACLE_ID = 4

MARKET_1X2 = "1x2"
MARKET_HHAD = "hhad"

SEL_1X2 = ("主胜", "平", "客胜")
SEL_HHAD = ("让胜", "让平", "让负")

_DDL = [
    """CREATE TABLE IF NOT EXISTS odds_snapshots (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        uniq_key VARCHAR(160) NOT NULL COMMENT '幂等键: 抓取分钟|fixture|销售日|编号|玩法|选项',
        fetched_at DATETIME NOT NULL COMMENT '本次抓取时间(截到分钟)',
        business_date DATE NULL COMMENT '竞彩销售日',
        kickoff_at DATETIME NULL COMMENT '开球时间',
        fixture_id BIGINT NULL COMMENT '关联 fixtures.id(仅 jc_map 当日有值)',
        jc_no VARCHAR(16) NULL COMMENT '竞彩编号 如 周日018',
        match_label VARCHAR(128) NULL COMMENT '主队 vs 客队(体彩简称)',
        league VARCHAR(64) NULL,
        market VARCHAR(16) NOT NULL COMMENT '1x2/hhad',
        selection VARCHAR(32) NOT NULL COMMENT '主胜/平/客胜 或 让胜/让平/让负',
        line_val DECIMAL(5,2) NULL COMMENT '竞彩 goalLine(负=主队让球)',
        odds DECIMAL(10,3) NULL COMMENT '竞彩原始赔率',
        jc_prob DECIMAL(6,4) NULL COMMENT '竞彩去水概率',
        jc_payback DECIMAL(6,4) NULL COMMENT '竞彩返还率',
        pin_fair DECIMAL(6,4) NULL COMMENT 'Pinnacle去水公平概率(让球行由公式还原)',
        pin_note VARCHAR(128) NULL COMMENT '公平价还原依据(用了哪几条腿)',
        pin_update DATETIME NULL COMMENT 'Pinnacle快照时间戳',
        ev DECIMAL(8,4) NULL COMMENT 'odds * pin_fair - 1',
        UNIQUE KEY uk_row (uniq_key),
        KEY idx_fetch (fetched_at),
        KEY idx_fixture (fixture_id),
        KEY idx_bdate (business_date),
        KEY idx_ev (ev)
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


# ──── 锐盘：取 Pinnacle 的盘口 ────

def _parse_ah_label(label: str):
    """'Home -1.5' -> ('Home', -1.5)；解析不了返回 None。"""
    parts = (label or "").split()
    if len(parts) != 2 or parts[0] not in ("Home", "Away"):
        return None
    try:
        return parts[0], float(parts[1])
    except (TypeError, ValueError):
        return None


def fetch_pinnacle(fixture_id: int) -> dict | None:
    """取一个赛程的 Pinnacle 盘口。

    返回 {"mw": 去水后的[主,平,客], "ah": {(side, line): 去水概率}, "update": datetime}
    取不到返回 None。
    """
    # ⚠️ api_get 重试 3 次后是 **raise**（不是返回 None）。不接住的话一个赛程
    # SSL 握手超时就会掀掉整轮采集 —— 实测 027 崩过整张表。这里降级成「这场没配上」，
    # 其余赛程照常走。
    try:
        data = api_get("/odds", {"fixture": fixture_id}, context=f"锐盘 {fixture_id}")
    except Exception as e:
        logger.warning(f"锐盘 {fixture_id} 取数失败，跳过该场: {e}")
        return None
    if check_api_error(data, f"取锐盘 {fixture_id}"):
        return None
    resp = data.get("response") or []
    if not resp:
        return None
    entry = resp[0]
    book = next((b for b in entry.get("bookmakers") or [] if b.get("id") == PINNACLE_ID), None)
    if not book:
        return None

    mw_raw, ah_raw = {}, {}
    for bet in book.get("bets") or []:
        name = bet.get("name")
        for v in bet.get("values") or []:
            try:
                odd = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            if odd <= 1.0:
                continue
            if name == "Match Winner":
                mw_raw[v.get("value")] = odd
            elif name == "Asian Handicap":
                parsed = _parse_ah_label(v.get("value"))
                if parsed:
                    ah_raw[parsed] = odd

    if not all(k in mw_raw for k in ("Home", "Draw", "Away")):
        return None

    inv = [1.0 / mw_raw[k] for k in ("Home", "Draw", "Away")]
    s = sum(inv)
    mw = [x / s for x in inv]

    # 每条让球线各自两路去水
    # ⚠️ 是【同号成对】挂的：`Home -0.75` 与 `Away -0.75` 才是同一个盘的两边
    # （主队让 0.75）。不要去配 `Home -0.75` + `Away +0.75`。
    ah = {}
    lines = {ln for (_side, ln) in ah_raw}
    for ln in lines:
        h, a = ah_raw.get(("Home", ln)), ah_raw.get(("Away", ln))
        if not h or not a:
            continue
        s2 = 1.0 / h + 1.0 / a
        ah[("Home", ln)] = (1.0 / h) / s2

    upd = None
    raw_upd = entry.get("update")
    if raw_upd:
        try:
            upd = datetime.fromisoformat(str(raw_upd).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            upd = None

    return {"mw": mw, "ah": ah, "update": upd}


def _p_ge(k: int, pin: dict):
    """P(净胜球 >= k)，从 Pinnacle 取。返回 (概率, 依据说明) 或 (None, None)。"""
    if k == 1:
        return pin["mw"][0], "MW_H"
    if k == 0:
        return 1.0 - pin["mw"][2], "1-MW_A"
    line = -(k - 0.5)
    p = pin["ah"].get(("Home", line))
    if p is None:
        return None, None
    return p, f"AH_H{line:+g}"


def hhad_fair(g: float, pin: dict):
    """竞彩整数让球线 g 的三路公平价。返回 {选项: (概率, 依据)}，取不全返回 {}。"""
    p_hi, n_hi = _p_ge(1 - g, pin)      # P(m >= 1-g)  -> 让胜边界
    p_lo, n_lo = _p_ge(-g, pin)         # P(m >= -g)   -> 让负边界
    if p_hi is None or p_lo is None:
        return {}
    return {
        "让胜": (p_hi, n_hi),
        "让平": (p_lo - p_hi, f"{n_lo}-{n_hi}"),
        "让负": (1.0 - p_lo, f"1-{n_lo}"),
    }


# ──── 竞彩 + 锐盘 -> 快照行 ────

def build_rows(payload: dict, fetch_pin: bool = True) -> tuple[list[dict], dict]:
    """把 /api/jc/matches 的 payload 转成快照行。

    返回 (rows, stats)。stats 用于打印「本次覆盖了几个销售日、几场配对上了」。
    """
    now = datetime.now().replace(second=0, microsecond=0)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    days = payload.get("days") or []

    rows: list[dict] = []
    stats = {"days": 0, "matches": 0, "paired": 0, "pin_fail": 0, "rows": 0,
             "unmapped": 0, "bdates": [], "ko_list": []}
    pin_cache: dict[int, dict | None] = {}

    for day in days:
        bdate = day.get("business_date")
        matches = day.get("matches") or []
        if not matches:
            continue
        stats["days"] += 1
        stats["matches"] += len(matches)
        stats["bdates"].append(bdate)

        for m in matches:
            fid = m.get("fixture_id")
            jc_no = m.get("match_num") or ""
            label = f"{m.get('home', '')} vs {m.get('away', '')}"[:128]
            kickoff = _parse_kickoff(m.get("kickoff"))
            if not fid:
                stats["unmapped"] += 1
                if kickoff:
                    stats["ko_list"].append(kickoff)

            pin = None
            if fetch_pin and fid:
                if fid not in pin_cache:
                    pin_cache[fid] = fetch_pinnacle(fid)
                pin = pin_cache[fid]
                if pin:
                    stats["paired"] += 1
                else:
                    stats["pin_fail"] += 1

            base = {
                "fetched_at": now, "business_date": bdate, "kickoff_at": kickoff,
                "fixture_id": fid, "jc_no": jc_no, "match_label": label,
                "league": (m.get("league") or "")[:64],
                "pin_update": pin["update"] if pin else None,
            }

            # 胜平负
            had = m.get("had") or None
            if had:
                fair = pin["mw"] if pin else None
                for i, sel in enumerate(SEL_1X2):
                    rows.append(_row(base, stamp, MARKET_1X2, sel, had,
                                     fair[i] if fair else None,
                                     ["MW_H", "MW_D", "MW_A"][i] if fair else None))

            # 让球胜平负
            hhad = m.get("hhad") or None
            if hhad:
                g = hhad.get("line")
                fair_map = hhad_fair(g, pin) if (pin and g is not None) else {}
                for sel in SEL_HHAD:
                    fv, note = fair_map.get(sel, (None, None))
                    rows.append(_row(base, stamp, MARKET_HHAD, sel, hhad, fv, note))

    stats["rows"] = len(rows)
    return rows, stats


def _parse_kickoff(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def _row(base, stamp, market, selection, pool, fair, note):
    odds = pool.get("h" if selection in ("主胜", "让胜") else
                    ("d" if selection in ("平", "让平") else "a"))
    jc_prob = pool.get({"主胜": "p_h", "平": "p_d", "客胜": "p_a",
                        "让胜": "p_h", "让平": "p_d", "让负": "p_a"}[selection])
    line = pool.get("line") if market == MARKET_HHAD else None
    ev = (odds * fair - 1.0) if (odds and fair is not None) else None

    row = dict(base)
    row.update({
        "uniq_key": f"{stamp}|{base['fixture_id'] or 0}|{base['business_date']}"
                    f"|{base['jc_no']}|{market}|{selection}"[:160],
        "market": market,
        "selection": selection,
        "line_val": line,
        "odds": odds,
        "jc_prob": jc_prob,
        "jc_payback": pool.get("payback"),
        "pin_fair": fair,
        "pin_note": (note or "")[:128] or None,
        "ev": ev,
    })
    return row


# ──── 落库 ────

_INSERT = """INSERT INTO odds_snapshots
    (uniq_key, fetched_at, business_date, kickoff_at, fixture_id, jc_no, match_label,
     league, market, selection, line_val, odds, jc_prob, jc_payback,
     pin_fair, pin_note, pin_update, ev)
    VALUES
    (:uniq_key, :fetched_at, :business_date, :kickoff_at, :fixture_id, :jc_no, :match_label,
     :league, :market, :selection, :line_val, :odds, :jc_prob, :jc_payback,
     :pin_fair, :pin_note, :pin_update, :ev)
    ON DUPLICATE KEY UPDATE
        odds=VALUES(odds), jc_prob=VALUES(jc_prob), jc_payback=VALUES(jc_payback),
        pin_fair=VALUES(pin_fair), pin_note=VALUES(pin_note),
        pin_update=VALUES(pin_update), ev=VALUES(ev)"""


def save_snapshot(rows: list[dict], db=None) -> int:
    """写入快照行。uniq_key 幂等 —— 同一分钟内重跑不会翻倍。"""
    if not rows:
        return 0
    own = db is None
    if own:
        db = SessionLocal()
    try:
        db.execute(text(_INSERT), rows)
        db.commit()
        return len(rows)
    finally:
        if own:
            db.close()


def latest_fetch(db=None):
    """最近一次快照时间，用于面板顶部的「数据新鲜度」告警。"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        return db.execute(text("SELECT MAX(fetched_at) FROM odds_snapshots")).scalar()
    finally:
        if own:
            db.close()


# ──── 采集入口（CLI 与定时任务共用） ────

def collect(dry_run: bool = False, fetch_pin: bool = True) -> tuple[list[dict], dict]:
    """抓一轮竞彩 + 锐盘。dry_run=True 只算不落库。"""
    payload = _build_payload()
    rows, stats = build_rows(payload, fetch_pin=fetch_pin)
    if not dry_run:
        stats["saved"] = save_snapshot(rows)
    else:
        stats["saved"] = 0
    return rows, stats


def diagnose(stats: dict) -> list[str]:
    """配不上锐盘时，区分两种【完全不同】的原因 —— 混为一谈会让人白折腾。

    ① 场次不在库里 —— 该日 fixture 尚未同步，或联赛 enabled=False（如 EFL Trophy id=46）
    ② 在库里却没配上一 —— 匹配判不出（队名对不上/窗内平手），写 overrides 即可

    2026-09-24 起 fixture_id 由 jc_match_service 逐场现场解析，不再有
    「jc_map.json 过期」这一说 —— 那个分支连同它的重建建议一并删掉了。

    ① 是 [[football-apifootball-limits]] 里记过的同一类现象：kickoff_matched=0 与
    fixture_id 缺失同因同源，先看上一条再判断，别当 bug 追。
    """
    out = []
    if not stats.get("matches"):
        return ["体彩这轮没开盘（空 matchInfoList 是合法状态，深夜必现）"]
    if stats.get("paired"):
        return out

    # 先查这些【具体开球时刻】库里有没有对手盘 —— 决定「是缺数据还是判不出」
    near = _fixtures_near(stats.get("ko_list") or [])
    unmapped = stats.get("unmapped") or 0

    if not near:
        out.append(f"{unmapped} 场配不上 fixture_id，且这些开球时刻在库里【一场都找不到】")
        _add_cause(out, stats)
    else:
        out.append(f"{unmapped} 场配不上 fixture_id，但其中 {near} 场的开球时刻库里其实有货")
        out.append("  → 是匹配没判出来，不是缺数据。判据见每场的 fixture_match 字段。")
        out.append("  → 仍判不出的写 tools/jc_overrides.json（key=\"<销售日> <体彩编号>\"）。")
    return out


def _add_cause(out: list[str], stats: dict):
    out.append("  真实原因二选一：")
    out.append("   a) 这些场次的联赛 enabled=False（例如 EFL Trophy id=46），fixtures 从未同步")
    out.append("   b) 该日 fixture 还没同步 -> POST /api/scheduler/fixtures/live")
    out.append("  这类场次本就没进模型、也没下注，跳过不影响测量。")


# fixtures.date 存的就是北京时间 —— 与竞彩给的开球时间同口径，不用换算。
# 这里原先减了 8 小时（以为存 UTC），结果把正确候选全甩出窗外，诊断于是在
# 「库里明明有货」的场次上斩钉截铁地说「一场都找不到」。2026-09-24 实测校正：
# 哥伦比亚的比赛存 08:30（当地 19:30）、巴西州联赛存 06:00（当地 19:00），
# 只有按北京时间才解释得通。
# 匹配容差：jc_match_service.py 用的是 ±600s，这里放宽到 ±1h 只作「库里有没有货」的判断
_MATCH_TOL = timedelta(hours=1)


def _fixtures_near(kickoffs: list) -> int:
    """给定的开球时刻里，有几个能在库里找到附近的 fixture。

    这才是「重建 jc_map 有没有用」的正确判据 —— 不是「那天库里有没有比赛」，
    而是「这些【具体开球时刻】有没有对手盘」。9-23 有一场美职联不能说明
    9-22 那三场英锦标赛配得上。
    """
    kos = [k for k in kickoffs if k]
    if not kos:
        return 0
    db = SessionLocal()
    try:
        n = 0
        for ko in kos:
            hit = db.execute(
                text("SELECT COUNT(*) FROM fixtures "
                     "WHERE date BETWEEN :lo AND :hi"),
                {"lo": ko - _MATCH_TOL, "hi": ko + _MATCH_TOL},
            ).scalar() or 0
            if hit:
                n += 1
        return n
    except Exception as e:  # 查不动就当不知道，别把诊断本身变成崩溃源
        logger.warning(f"诊断查询失败: {e}")
        return 0
    finally:
        db.close()


def odds_watch_job(db=None):
    """scheduler 入口：fn(db) 签名，与 weekly_review_job 一致。"""
    rows, stats = collect()
    logger.info(
        f"赔率快照: 覆盖 {stats['days']} 个销售日 / {stats['matches']} 场，"
        f"锐盘配对 {stats['paired']} 场(失败 {stats['pin_fail']})，入库 {stats['saved']} 行"
    )
    for msg in diagnose(stats):
        logger.error(f"赔率快照: {msg}")
    return stats
