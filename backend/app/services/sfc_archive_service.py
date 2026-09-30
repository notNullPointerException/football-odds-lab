# -*- coding: utf-8 -*-
"""胜负彩 14 场【按期存档 + 每期复盘】。

为什么必须存档：胜负彩按【期号】卖，上游 getFootBallMatchV1.qry 只返回【当前期】。
一期停售、开奖、翻到下一期之后，这 14 场的对阵、赔率、彩果就再也取不回来了 ——
「每期错了几场」不是页面没做，是数据没了。所以每期都得先落库。

三条定死的设计（改之前先读完）：

1. 🔴 冻结的是【赛前快照】，不是开奖后再去读 predictions。
   赛后重跑会被污染：_fetch_team_recent_via_api（prediction/predict.py:216）没有日期
   过滤，会把【本场比分】喂进 LLM 的 prompt（实测 fixture 1528878）。开奖后才去读，
   读到的就是这种被污染的值。

2. 🔴 头块用 COALESCE 的【填空白】语义，不是首次写死、也不是每次覆盖。
   · 纯首次写死不行：常见流程是先开 SFC 页（那时 14 场全「未跑」）→ 再点「跑 14 场
     预测」，快照会永远停在 NULL。
   · 纯覆盖不行：赛后重跑会把污染值盖进赛前记录。
   与 predict.py:772 给赔率列用 COALESCE 是同一套思路。

3. 🔴 每个头各有各的分母。体彩没开赔率的场次（实测 14 场里有 5 场）had 为空，
   该场不进庄家盘/体彩盘的分母，也不进没跑过预测的头。卡片上写「错 5/12」，
   不能拿 14 硬除。

建表不进 alembic（与 bet_journal / std_line / odds_snapshots 同思路，见
bet_journal_service.py 的 _DDL 注释）。
"""
import json
import threading
from datetime import datetime
from math import exp, factorial

from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal

# ── 彩果/比分口径 ────────────────────────────────────────────────────────
# 胜负彩只猜【90 分钟含伤停补时】的胜平负，不看加时和点球。
# 判「这场算不算完」只能看 status_short —— goals_home 有值≠踢完了（进行中就有值）。
FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}

# 胜负彩的彩果记法：3=主胜 1=平 0=客胜
_DIR_OF_RESULT = {"3": "home", "1": "draw", "0": "away"}

# 泊松求和上限与夹取范围，与 prediction/predict.py:70-71 的 _poisson_top3 对齐。
# 这里用纯 math 手写而不引 scipy —— app/services 里不值得为 30 行算术拖一个科学计算库。
_MAX_GOALS = 8
_LAM_LO, _LAM_HI = 0.1, 8.0


_DDL = [
    """CREATE TABLE IF NOT EXISTS sfc_issues (
        issue VARCHAR(16) PRIMARY KEY COMMENT '期号, 如 26134',
        lottery_name VARCHAR(32) NULL COMMENT '彩票名称, 胜负游戏',
        sale_begin DATETIME NULL COMMENT '开售时间',
        sale_end DATETIME NULL COMMENT '整期停售时间',
        draw_time DATETIME NULL COMMENT '开奖时间',
        total_count INT NULL COMMENT '本期场次数(上游不给, 本地 count)',
        first_kickoff DATETIME NULL COMMENT '14 场里最早开球',
        last_kickoff DATETIME NULL COMMENT '14 场里最晚开球',
        archived_at DATETIME NULL COMMENT '首次【赛前】存档时刻; 上游历史导入的行留 NULL(见下)',
        total_sale_amount DECIMAL(14,2) NULL COMMENT '本期总销量(胜负彩), 只有历史导入填',
        rj_sale_amount DECIMAL(14,2) NULL COMMENT '任选9场销量, 【另一个奖池】别与上面相加, 只有历史导入填',
        lottery_prize_json TEXT NULL COMMENT '奖级奖金原始 JSON, 只有历史导入填',
        lottery_draw_result VARCHAR(64) NULL COMMENT '开奖号原文 14 个(3/1/0/*), 只有历史导入填',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS sfc_issue_matches (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        issue VARCHAR(16) NOT NULL COMMENT '关联 sfc_issues.issue',
        no INT NOT NULL COMMENT '场次号 1..14',
        fixture_id BIGINT NULL COMMENT '关联 fixtures.id, 编不出为 NULL',
        infohub_match_id BIGINT NULL COMMENT '体彩 infohubMatchId; 历史期唯一能用的回溯锚点(见 sfc_history.py)',
        league VARCHAR(64) NULL,
        home VARCHAR(64) NULL,
        away VARCHAR(64) NULL,
        match_date VARCHAR(16) NULL COMMENT '体彩 startTime, 只有日期没有时间',
        kickoff_at DATETIME NULL COMMENT '取自 fixtures.date(本地 UTC+8), 判「赛前/赛后」用',
        had_h DECIMAL(7,3) NULL COMMENT '胜负彩自己的 h = 体彩盘',
        had_d DECIMAL(7,3) NULL,
        had_a DECIMAL(7,3) NULL,
        mkt_h DECIMAL(7,3) NULL COMMENT '庄家盘 Pinnacle 欧赔',
        mkt_d DECIMAL(7,3) NULL,
        mkt_a DECIMAL(7,3) NULL,
        prob_home DECIMAL(6,4) NULL COMMENT 'xgb 头, 赛前快照, 填空白不覆盖',
        prob_draw DECIMAL(6,4) NULL,
        prob_away DECIMAL(6,4) NULL,
        lam_home DECIMAL(6,3) NULL COMMENT 'lambda 头入参, 赛前快照',
        lam_away DECIMAL(6,3) NULL,
        llm_win VARCHAR(16) NULL COMMENT 'LLM 头: 主胜/平局/客胜',
        llm_score VARCHAR(16) NULL,
        llm_win_pct VARCHAR(16) NULL,
        model_group VARCHAR(32) NULL,
        predict_at DATETIME NULL COMMENT 'predictions.created_at, 首次预测时刻',
        snap_at DATETIME NULL COMMENT '本行快照写入时刻',
        cz_result CHAR(1) NULL COMMENT '存档时体彩给的官方彩果 3/1/0',
        cz_score VARCHAR(16) NULL COMMENT '存档时体彩给的比分 2:1',
        cz_half_score VARCHAR(16) NULL COMMENT '半场比分 0:2, 只有历史导入填',
        UNIQUE KEY uk_row (issue, no),
        KEY idx_fixture (fixture_id),
        KEY idx_infohub (infohub_match_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

# 🔴 给【已存在】的表补列。CREATE TABLE IF NOT EXISTS 对已存在的表是【空操作】——
# 上面 _DDL 里新加的列在老库上永远长不出来，必须显式 ALTER。上面两处 DDL 和这里
# 必须同步维护：DDL 管新库，这里管老库，少一边就有一半环境是坏的。
# MySQL 没有 `ADD COLUMN IF NOT EXISTS`（那是 MariaDB 的），只能先查 information_schema。
_NEW_COLUMNS = [
    ("sfc_issues", "total_sale_amount",
     "DECIMAL(14,2) NULL COMMENT '本期总销量(胜负彩), 只有历史导入填'"),
    ("sfc_issues", "rj_sale_amount",
     "DECIMAL(14,2) NULL COMMENT '任选9场销量, 【另一个奖池】别与上面相加, 只有历史导入填'"),
    ("sfc_issues", "lottery_prize_json",
     "TEXT NULL COMMENT '奖级奖金原始 JSON, 只有历史导入填'"),
    ("sfc_issues", "lottery_draw_result",
     "VARCHAR(64) NULL COMMENT '开奖号原文 14 个(3/1/0/*), 只有历史导入填'"),
    ("sfc_issue_matches", "infohub_match_id",
     "BIGINT NULL COMMENT '体彩 infohubMatchId; 历史期唯一能用的回溯锚点(见 sfc_history.py)'"),
    ("sfc_issue_matches", "cz_half_score",
     "VARCHAR(16) NULL COMMENT '半场比分 0:2, 只有历史导入填'"),
]

# 补列后要建的索引（列是后加的，索引也就不是 CREATE TABLE 带出来的）
_NEW_INDEXES = [
    ("sfc_issue_matches", "idx_infohub", "infohub_match_id"),
]


def _ensure_columns(db):
    """幂等补列 + 补索引。跑多少次都只做「缺的那部分」。"""
    for table, col, spec in _NEW_COLUMNS:
        n = db.execute(text(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = :t AND COLUMN_NAME = :c"""),
            {"t": table, "c": col}).scalar()
        if not n:
            # 表名/列名/类型全是本模块的常量，不含外部输入
            db.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {spec}"))
            logger.info(f"sfc: 补列 {table}.{col}")
    for table, idx, col in _NEW_INDEXES:
        n = db.execute(text(
            """SELECT COUNT(*) FROM information_schema.STATISTICS
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = :t AND INDEX_NAME = :i"""),
            {"t": table, "i": idx}).scalar()
        if not n:
            db.execute(text(f"ALTER TABLE `{table}` ADD INDEX `{idx}` (`{col}`)"))
            logger.info(f"sfc: 补索引 {table}.{idx}")

# 一句话说明存档频率：见 app/api/sfc.py 的 120 秒防抖。
_ENSURED = False
_ENSURE_LOCK = threading.Lock()


def ensure_tables():
    """建表 + 补列。CREATE TABLE IF NOT EXISTS 与 _ensure_columns 都是幂等的。"""
    global _ENSURED
    db = SessionLocal()
    try:
        for ddl in _DDL:
            db.execute(text(ddl))
        _ensure_columns(db)
        db.commit()
        _ENSURED = True
    finally:
        db.close()


def _ensure_once():
    """确认表存在（首次调用才真去建）。

    🔴 仓库里 ensure_tables() 原本只在 tools/*.py 的 init/build 子命令里调，没有启动
    钩子。但存档是【打开页面就自动发生】的，用户不会为了这个去跑一次 CLI —— 所以这里
    用模块级标志 + 双检（与 predict.py 的 _fetch_jc_odds 同一个写法）在首次用到时补建。
    两道检查都要：等锁的线程进来时，先进去的那个可能已经建完了。
    """
    if _ENSURED:
        return
    with _ENSURE_LOCK:
        if _ENSURED:
            return
        ensure_tables()


# ── 小工具 ──────────────────────────────────────────────────────────────
def _num(v):
    """转 float；转不了返回 None（空串 / None / NaN 都走这里）。

    ⚠️ 不要写成 `float(v or 0)` —— 赔率列的空值是「这场没开这个玩法」，
    当成 0 会让 1/0 炸掉，或者让空赔率在 argmax 里胜出。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 排除 NaN


def _dt(v):
    """体彩给的是字符串（"2026-09-28 20:30:00"），换 DATETIME；认不出就 None。

    宁可存 NULL 也不猜：开奖时间只是卡片上的一行提示，猜错反而误导。
    """
    if v is None or isinstance(v, datetime):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


def _argmax3(h, d, a):
    """三个数取最大项，返回 "home"/"draw"/"away"；【三个都是 None 返回 None】。

    🔴 全空必须返回 None：max() 在空值上会挑出第一个，页面就会把「这场没开赔率」
    显示成「主胜」—— 一个看起来正常、实际凭空捏造的预测。jc.py:279 踩过同一个坑。

    完全打平时（如 λ 两边相等）取列表里靠前的（home > draw > away），这是【刻意定的
    约定】不是 bug —— 否则同样的输入会随字典/集合顺序飘。实战里三头几乎不打平。
    """
    vals = [(_num(h), "home"), (_num(d), "draw"), (_num(a), "away")]
    vals = [(v, k) for v, k in vals if v is not None]
    if not vals:
        return None
    return max(vals, key=lambda t: t[0])[1]


def _argmax_inv(h, d, a):
    """赔率取 argmax —— 比的是【概率】，所以先把赔率取倒数再比。

    不需要调 _devig：任何单调归一化（等量减法除外，它不单调）都不改变 argmax，
    直接比 1/赔率 与去水后的结论完全一致，还少一次"到底用哪个公式"的争论。
    """
    out = []
    for v in (h, d, a):
        n = _num(v)
        # 赔率 0 / 负数 / 空都当「这个选项没有」—— 1/0 会炸，1/负数 会算出个负概率
        out.append(1.0 / n if n and n > 0 else None)
    return _argmax3(*out)


def _poisson_1x2(lh, la):
    """由 lambda 推 1x2 概率（主胜/平/客胜），返回三者之和为 1 的三元组。

    🔴 λ 头【必须】走这一步，不能只比 lam_home > lam_away。后者把平局整个丢掉，
    而且小比分平局（1-1、0-0）在足球里占比很高，只比大小会把 λ 头的命中率算得虚低
    —— 复盘出来的「λ 最差」有相当一部分是这个算法造成的，不是模型造成的。
    """
    lh = max(_LAM_LO, min(float(lh), _LAM_HI))
    la = max(_LAM_LO, min(float(la), _LAM_HI))
    ph = pd = pa = 0.0
    for i in range(_MAX_GOALS + 1):
        pi = exp(-lh) * lh ** i / factorial(i)
        for j in range(_MAX_GOALS + 1):
            p = pi * (exp(-la) * la ** j / factorial(j))
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def _lam_pick(lh, la):
    if _num(lh) is None or _num(la) is None:
        return None
    return _argmax3(*_poisson_1x2(lh, la))


def _llm_pick(win):
    """"主胜"/"平局"/"客胜" -> home/draw/away。认不出返回 None（不猜）。"""
    s = str(win or "")
    if "主" in s:
        return "home"
    if "平" in s:
        return "draw"
    if "客" in s:
        return "away"
    return None


# ── 存档 ────────────────────────────────────────────────────────────────
_ISSUE_UPSERT = """INSERT INTO sfc_issues
    (issue, lottery_name, sale_begin, sale_end, draw_time, total_count,
     first_kickoff, last_kickoff, archived_at)
    VALUES (:issue, :lottery_name, :sale_begin, :sale_end, :draw_time, :total_count,
            :first_kickoff, :last_kickoff, NOW())
    ON DUPLICATE KEY UPDATE
        lottery_name=VALUES(lottery_name),
        sale_begin=COALESCE(VALUES(sale_begin), sale_begin),
        sale_end=COALESCE(VALUES(sale_end), sale_end),
        draw_time=COALESCE(VALUES(draw_time), draw_time),
        total_count=VALUES(total_count),
        first_kickoff=VALUES(first_kickoff), last_kickoff=VALUES(last_kickoff)"""

# 🔴 头块（prob_*/lam_*/llm_*）走 COALESCE(旧值, 新值) = 填空白、不覆盖。
#    其余列（对阵、赔率、彩果）一律 =VALUES() 覆盖：赔率会浮动、彩果是后来才有的，
#    都要以最新为准。fixture_id 特殊：编不出来是 NULL，别让一次失败把已解析的 id 抹掉。
_MATCH_UPSERT = """INSERT INTO sfc_issue_matches
    (issue, no, fixture_id, league, home, away, match_date, kickoff_at,
     had_h, had_d, had_a, mkt_h, mkt_d, mkt_a,
     prob_home, prob_draw, prob_away, lam_home, lam_away,
     llm_win, llm_score, llm_win_pct, model_group, predict_at, snap_at,
     cz_result, cz_score)
    VALUES
    (:issue, :no, :fixture_id, :league, :home, :away, :match_date, :kickoff_at,
     :had_h, :had_d, :had_a, :mkt_h, :mkt_d, :mkt_a,
     :prob_home, :prob_draw, :prob_away, :lam_home, :lam_away,
     :llm_win, :llm_score, :llm_win_pct, :model_group, :predict_at, NOW(),
     :cz_result, :cz_score)
    ON DUPLICATE KEY UPDATE
        fixture_id=COALESCE(VALUES(fixture_id), fixture_id),
        league=COALESCE(VALUES(league), league), home=VALUES(home), away=VALUES(away),
        match_date=COALESCE(VALUES(match_date), match_date),
        kickoff_at=COALESCE(VALUES(kickoff_at), kickoff_at),
        had_h=COALESCE(VALUES(had_h), had_h), had_d=COALESCE(VALUES(had_d), had_d),
        had_a=COALESCE(VALUES(had_a), had_a),
        mkt_h=VALUES(mkt_h), mkt_d=VALUES(mkt_d), mkt_a=VALUES(mkt_a),
        prob_home=COALESCE(prob_home, VALUES(prob_home)),
        prob_draw=COALESCE(prob_draw, VALUES(prob_draw)),
        prob_away=COALESCE(prob_away, VALUES(prob_away)),
        lam_home=COALESCE(lam_home, VALUES(lam_home)),
        lam_away=COALESCE(lam_away, VALUES(lam_away)),
        llm_win=COALESCE(llm_win, VALUES(llm_win)),
        llm_score=COALESCE(llm_score, VALUES(llm_score)),
        llm_win_pct=COALESCE(llm_win_pct, VALUES(llm_win_pct)),
        model_group=COALESCE(model_group, VALUES(model_group)),
        predict_at=COALESCE(predict_at, VALUES(predict_at)),
        cz_result=COALESCE(VALUES(cz_result), cz_result),
        cz_score=COALESCE(VALUES(cz_score), cz_score)"""


def _kickoff_map(db, fids: list) -> dict:
    """fixture_id -> fixtures.date（本地 UTC+8 的裸 datetime）。

    🔴 fixtures.date 存的是本地时间（UNIX_TIMESTAMP('2026-09-24 00:00:00') == timestamp），
    而 predictions.created_at 是 datetime.now() 也是本地 —— 两边同为本地裸值，可以直接比。
    别在这里"顺手"加 8 小时。
    """
    ids = [i for i in dict.fromkeys(fids) if i]
    if not ids:
        return {}
    ph = ",".join(f":f{i}" for i in range(len(ids)))
    rows = db.execute(text(
        f"SELECT id, date FROM fixtures WHERE id IN ({ph})"),
        {f"f{i}": v for i, v in enumerate(ids)}).fetchall()
    return {r[0]: r[1] for r in rows}


def _predict_map(db, fids: list) -> dict:
    """fixture_id -> predictions.created_at（【首次】预测时刻，ON DUPLICATE 不更新）。

    不用 updated_at：重跑会刷新它，赛后重跑就看不出来了。created_at 天然标记首次。
    """
    ids = [i for i in dict.fromkeys(fids) if i]
    if not ids:
        return {}
    ph = ",".join(f":f{i}" for i in range(len(ids)))
    rows = db.execute(text(
        f"SELECT fixture_id, created_at FROM predictions WHERE fixture_id IN ({ph})"),
        {f"f{i}": v for i, v in enumerate(ids)}).fetchall()
    return {r[0]: r[1] for r in rows}


def archive_payload(payload: dict) -> dict:
    """把 /api/sfc/matches 的 payload 落库。返回本次写入的计数，给按钮回显用。

    只做 upsert，不做判分 —— 判分是读的时候现算的（见 get_issue_review 的说明）。
    """
    _ensure_once()

    issue = str(payload.get("issue") or "").strip()
    matches = payload.get("matches") or []
    if not issue or not matches:
        # 没期号/没场次 = 上游这一刻没有可存档的内容（空 ≠ 失败，见 sfc.py 的同款注释）
        return {"issue": issue or None, "matches": 0, "with_pred": 0, "with_fixture": 0}

    fids = [m.get("fixture_id") for m in matches]
    db = SessionLocal()
    try:
        kicks = _kickoff_map(db, fids)
        preds = _predict_map(db, fids)

        rows = []
        for m in matches:
            fid = m.get("fixture_id")
            had = m.get("had") or {}
            book = m.get("book") or {}
            pred = m.get("pred") or {}
            prob = pred.get("prob") or {}
            lam = pred.get("lambda") or {}
            llm = m.get("llm") or {}
            res = (m.get("result") or "").strip() or None
            rows.append({
                "issue": issue,
                "no": m.get("no"),
                "fixture_id": fid,
                "league": m.get("league") or None,
                "home": m.get("home") or "",
                "away": m.get("away") or "",
                "match_date": (m.get("match_date") or "") or None,
                "kickoff_at": kicks.get(fid),
                "had_h": _num(had.get("h")), "had_d": _num(had.get("d")),
                "had_a": _num(had.get("a")),
                "mkt_h": _num(book.get("h")), "mkt_d": _num(book.get("d")),
                "mkt_a": _num(book.get("a")),
                "prob_home": _num(prob.get("home")), "prob_draw": _num(prob.get("draw")),
                "prob_away": _num(prob.get("away")),
                "lam_home": _num(lam.get("home")), "lam_away": _num(lam.get("away")),
                "llm_win": llm.get("win") or None,
                "llm_score": llm.get("score") or None,
                "llm_win_pct": llm.get("win_pct") or None,
                "model_group": pred.get("model_group") or None,
                "predict_at": preds.get(fid),
                "cz_result": res if res in _DIR_OF_RESULT else None,
                "cz_score": (m.get("score") or "") or None,
            })

        kk = [r["kickoff_at"] for r in rows if r["kickoff_at"]]
        db.execute(text(_ISSUE_UPSERT), {
            "issue": issue,
            "lottery_name": payload.get("lottery_name") or None,
            "sale_begin": _dt(payload.get("sale_begin")),
            "sale_end": _dt(payload.get("sale_end")),
            "draw_time": _dt(payload.get("draw_time")),
            "total_count": len(rows),
            "first_kickoff": min(kk) if kk else None,
            "last_kickoff": max(kk) if kk else None,
        })
        db.execute(text(_MATCH_UPSERT), rows)
        db.commit()
    finally:
        db.close()

    return {
        "issue": issue,
        "matches": len(rows),
        "with_pred": sum(1 for r in rows if r["prob_home"] is not None),
        "with_fixture": sum(1 for r in rows if r["fixture_id"]),
    }


# ── 读 + 判分（全部在内存里算，不落库）────────────────────────────────────
# 为什么不把对错写回表：赛果是我们自己每天同步的 fixtures 导出来的，随时能重算；
# 存一份就等于多一份会过期的派生数据 —— 改口径时要记得回去重刷，忘了就静默错。
# 唯一必须落库的是体彩自己给的 cz_result（丢了就永远没有），它已经在表里了。

_ROWS_SQL = """SELECT m.*, f.status_short, f.goals_home, f.goals_away,
                      f.fulltime_home, f.fulltime_away
    FROM sfc_issue_matches m
    LEFT JOIN fixtures f ON f.id = m.fixture_id
    WHERE m.issue IN (%s)
    ORDER BY m.issue DESC, m.no ASC"""

# (键, 文案里用的名字)。名字里不留空格 —— 文案模板是 f"最准的是{label}（"，留了空格
# 就会变成「最准的是 λ 头（」而「最差的是 体彩盘（」这种一会儿有一会儿没有的排版。
_HEAD_LABELS = [("prob", "prob头"), ("lam", "λ头"), ("llm", "LLM头"),
                ("mkt", "庄家盘"), ("had", "体彩盘")]


def _row_outcome(r) -> dict:
    """一行比赛的【官方口径】赛果。

    🔴 优先用体彩的 cz_result，fixtures 只做兜底：
    cz_result 是这期彩票自己的彩果裁定（延期、取消怎么算都按体彩的规矩），
    是「错了没有」这个问题唯一的权威答案。fixtures 的价值是【可得性】——
    存档时还没开奖、或者用户后来没再打开页面，cz_result 就是 NULL，
    这时才用 fixtures 推，让复盘照样能算出来。
    比分口径：胜负彩猜的是 90 分钟含伤停补时，所以 fulltime 优先于 goals
    （api-football 的 goals 含加时；全库 FT 场次里两者只有 2 场不同，但口径必须对）。
    """
    cz = (r["cz_result"] or "").strip() or None
    fh, fa = r["fulltime_home"], r["fulltime_away"]
    if fh is None or fa is None:
        fh, fa = r["goals_home"], r["goals_away"]
    # 只有真正踢完了才敢从比分推彩果 —— status_short 是唯一判据，
    # goals_home 有值不代表结束（进行中就有值）
    ft_ok = (r["status_short"] in FINISHED) and fh is not None and fa is not None

    if cz:
        return {"result": cz, "src": "sporttery",
                "score": r["cz_score"] or (f"{fh}-{fa}" if ft_ok else None),
                "ft": (fh, fa) if ft_ok else None}
    if ft_ok:
        res = "3" if fh > fa else ("1" if fh == fa else "0")
        return {"result": res, "src": "fixtures", "score": f"{fh}-{fa}", "ft": (fh, fa)}
    return {"result": None, "src": None, "score": None, "ft": None}


def _picks(r) -> dict:
    """一行比赛五个头各自判成什么方向（未判为 None）。"""
    o = _row_outcome(r)
    return {
        "prob": _argmax3(r["prob_home"], r["prob_draw"], r["prob_away"]),
        "lam": _lam_pick(r["lam_home"], r["lam_away"]),
        "llm": _llm_pick(r["llm_win"]),
        "mkt": _argmax_inv(r["mkt_h"], r["mkt_d"], r["mkt_a"]),
        "had": _argmax_inv(r["had_h"], r["had_d"], r["had_a"]),
        "_outcome": o,
        "_dir": _DIR_OF_RESULT.get(o["result"]) if o["result"] else None,
    }


def _tally(rows: list) -> dict:
    """按头汇总：每个头的分母是【它自己有值且赛果已知】的场次，各不相同。"""
    heads = {}
    for key, label in _HEAD_LABELS:
        n = wrong = 0
        for r in rows:
            if r["_picks"]["_dir"] is None or r["_picks"][key] is None:
                continue
            n += 1
            if r["_picks"][key] != r["_picks"]["_dir"]:
                wrong += 1
        heads[key] = {"label": label, "n": n, "wrong": wrong,
                      "right": n - wrong,
                      "acc": round((n - wrong) / n, 4) if n else None}
    return heads


def _bullets(rows: list, heads: dict, meta: dict) -> list:
    """自动复盘要点。纯函数式的确定性推导，不调 LLM（用户 2026-09-28 拍板）。"""
    out = []
    scored = [r for r in rows if r["_picks"]["_dir"] is not None]
    if not scored:
        return ["本期还没有已出赛果的场次 —— 开奖后回来再看。"]

    # 🔴 排名比的是【对率】，不是「错几场」的绝对值 —— 各头分母天然不同（体彩没开赔率的
    #    场次不进市场两列的分母），拿 5 场的「错 1」去赢 12 场的「错 4」是错的。
    #    覆盖率不到最大覆盖面一半的头不参与排名，只列数字。
    ranked = [h for h in heads.values() if h["n"] and h["acc"] is not None]
    max_n = max((h["n"] for h in ranked), default=0)
    ranked = [h for h in ranked if h["n"] * 2 >= max_n] or ranked
    if ranked:
        best = max(ranked, key=lambda h: (h["acc"], h["n"]))
        worst = min(ranked, key=lambda h: (h["acc"], -h["n"]))
        if best["wrong"] != worst["wrong"]:
            out.append(f"本期最准的是{best['label']}"
                       f"（错 {best['wrong']}/{best['n']}，{best['acc'] * 100:.1f}% 对），"
                       f"最差的是{worst['label']}"
                       f"（错 {worst['wrong']}/{worst['n']}，{worst['acc'] * 100:.1f}% 对）。")
        else:
            out.append(f"各头本期错数相同，都是 {best['wrong']}/{best['n']}。")

    # 错在平局：赛果是平、但头判了胜负 —— 平局是这套模型的长期弱项，单独点出来
    draw_lost = [r for r in scored
                 if r["_picks"]["_dir"] == "draw"
                 and sum(1 for k, _ in _HEAD_LABELS
                         if r["_picks"][k] and r["_picks"][k] != "draw") >= 2]
    if draw_lost:
        nos = "、".join(str(r["no"]) for r in draw_lost)
        out.append(f"错在平局 {len(draw_lost)} 场（第 {nos} 场，多数头判了胜负）。")

    # 市场口径：庄家盘优先，没拉到的场次用体彩盘兜底
    def mkt_of(r):
        return r["_picks"]["mkt"] or r["_picks"]["had"]

    both = [r for r in scored if mkt_of(r)]
    if both:
        cold = [r for r in both if mkt_of(r) != r["_picks"]["_dir"]]
        model_right_mkt_wrong = [
            r for r in both
            if mkt_of(r) != r["_picks"]["_dir"]
            and any(r["_picks"][k] == r["_picks"]["_dir"] for k, _ in _HEAD_LABELS)]
        out.append(f"市场热门（庄家盘优先）错 {len(cold)}/{len(both)} 场；"
                   f"其中 {len(model_right_mkt_wrong)} 场【模型的头判对了而市场错】"
                   f"—— 这些是水可能真的在那里。")
        wrong_all = [r for r in both
                     if mkt_of(r) != r["_picks"]["_dir"]
                     and all(r["_picks"][k] != r["_picks"]["_dir"]
                             for k, _ in _HEAD_LABELS if r["_picks"][k])]
        if wrong_all:
            nos = "、".join(str(r["no"]) for r in wrong_all)
            out.append(f"真爆冷 {len(wrong_all)} 场（第 {nos} 场：五个头全错，市场也错）。")

    # 数据干净度：预测跑在开球之后的行，值可能已被本场比分污染
    late = [r for r in scored
            if r["predict_at"] and r["kickoff_at"] and r["predict_at"] >= r["kickoff_at"]]
    if late:
        nos = "、".join(str(r["no"]) for r in late)
        out.append(f"⚠️ 第 {nos} 场的预测跑在开球之后，表里的值可能已被本场比分污染"
                   f"（predict.py 的近期战绩接口没有日期过滤）—— 这几场别拿来当结论。")
    else:
        out.append("预测全部跑在开球之前，快照干净。")
    return out


def _match_out(r) -> dict:
    p = r["_picks"]
    o = p["_outcome"]
    return {
        "no": r["no"], "fixture_id": r["fixture_id"],
        "league": r["league"], "home": r["home"], "away": r["away"],
        "match_date": r["match_date"],
        "result": o["result"], "result_src": o["src"], "score": o["score"],
        "cz_result": r["cz_result"],
        "predict_at": r["predict_at"].strftime("%Y-%m-%d %H:%M") if r["predict_at"] else None,
        "kickoff_at": r["kickoff_at"].strftime("%Y-%m-%d %H:%M") if r["kickoff_at"] else None,
        "late": bool(r["predict_at"] and r["kickoff_at"]
                     and r["predict_at"] >= r["kickoff_at"]),
        # 每格 {pick: 方向, hit: true/false/null(无值或无赛果)}
        "cells": {k: {"pick": p[k],
                      "hit": (None if (p[k] is None or p["_dir"] is None)
                              else p[k] == p["_dir"])}
                  for k, _ in _HEAD_LABELS},
    }


def _money(v):
    """'2,034,299' -> 2034299。金额一律是整元，转 int。

    ⚠️ 不能复用 _num —— 上游带千分位逗号，float("2,034,299") 直接抛，_num 会返回 None，
    看起来就像"这期没奖金"。也**不能**写成 `str(v or "")`：上游用 "0" 表示该档 0 注
    （04028 的任选9场就是），`v or ""` 会把字符串 "0" 之外的整数 0 吞成空。
    """
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    try:
        return int(round(float(s)))
    except (TypeError, ValueError, OverflowError):
        # ⚠️ OverflowError 必须一起 catch：本函数的契约是「转不了返回 None」，
        # 而 float("inf") / float("1e400") 会让 round() 抛 OverflowError（不是 ValueError）。
        # 漏了它就不是「这期没奖金」，而是整个 /api/sfc/issues 500。
        return None


def _prizes(meta) -> list:
    """这一期的奖级：奖金 / 中奖注数 / 该档派奖总额。没有就 []，不是 None。

    🔴 奖级不是恒定的两档，历史上换过位置（分界实测是期号 19085）：
      · 04028~19085（含）：【任选9场】当第三档塞在 prizeLevelList 里
      · 19086 起：        任九改放另一个键 prizeLevelListRj，且两个 list 不会同时出现任九
    历史导入(tools/sfc_history.py)原先只读 prizeLevelList ⇒ 库里的任九**正好停在 19085**，
    看着与"2019 年年中停售"一模一样（我据此误判过一次，已作废）。
    现在 _prize_json 两边都读、按档位名去重后合并，所以新老行同形。
    """
    raw = meta.get("lottery_prize_json")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for x in items or []:
        lv = str(x.get("prizeLevel") or "").strip()
        if not lv:
            continue
        out.append({"level": lv,
                    "amount": _money(x.get("stakeAmount")),
                    "count": _money(x.get("stakeCount")),
                    "total": _money(x.get("totalPrizeamount"))})
    return out


def _issue_out(meta, rows) -> dict:
    heads = _tally(rows)
    scored = [r for r in rows if r["_picks"]["_dir"] is not None]
    return {
        "issue": meta["issue"],
        "lottery_name": meta["lottery_name"],
        "sale_end": meta["sale_end"].strftime("%Y-%m-%d %H:%M:%S") if meta["sale_end"] else None,
        "draw_time": meta["draw_time"].strftime("%Y-%m-%d %H:%M:%S") if meta["draw_time"] else None,
        "archived_at": meta["archived_at"].strftime("%Y-%m-%d %H:%M") if meta["archived_at"] else None,
        "total_count": meta["total_count"],
        "scored_count": len(scored),
        "sale_amount": _money(meta["total_sale_amount"]),
        "rj_sale_amount": _money(meta["rj_sale_amount"]),
        "prizes": _prizes(meta),
        "heads": heads,
        "head_order": [k for k, _ in _HEAD_LABELS],
        "bullets": _bullets(rows, heads, meta),
        "matches": [_match_out(r) for r in rows],
    }


def _load(db, issues: list) -> dict:
    """一次性把这批期号的所有行读回来（连同 fixtures），在内存里判分。"""
    if not issues:
        return {}
    ph = ",".join(f":i{i}" for i in range(len(issues)))
    rows = db.execute(text(_ROWS_SQL % ph),
                      {f"i{i}": v for i, v in enumerate(issues)}).mappings().all()
    metas = db.execute(text(
        f"""SELECT issue, lottery_name, sale_end, draw_time, archived_at, total_count,
                   total_sale_amount, rj_sale_amount, lottery_prize_json
            FROM sfc_issues WHERE issue IN ({ph})"""),
        {f"i{i}": v for i, v in enumerate(issues)}).mappings().all()
    meta_by_issue = {m["issue"]: m for m in metas}

    by_issue: dict = {i: [] for i in issues}
    for r in rows:
        r = dict(r)
        r["_picks"] = _picks(r)
        by_issue.setdefault(r["issue"], []).append(r)

    out = {}
    for i in issues:
        m = meta_by_issue.get(i)
        if m is None:
            continue
        out[i] = _issue_out(m, by_issue.get(i) or [])
    return out


def list_issues(page: int = 1, size: int = 20, date: str | None = None) -> dict:
    """归档过的期号列表（倒序，按停售时间排），带分页信封，与 /api/predictions 同形。"""
    _ensure_once()
    db = SessionLocal()
    try:
        where, params = "", {}
        if date:
            # 按【停售日】筛：一期就是"这天停售的那一期"，比按开奖日好理解
            where = "WHERE DATE(sale_end) = :d"
            params["d"] = date
        total = db.execute(text(f"SELECT COUNT(*) FROM sfc_issues {where}"), params).scalar() or 0
        page = max(1, int(page))
        size = max(1, min(int(size), 100))
        issues = [r[0] for r in db.execute(text(
            f"""SELECT issue FROM sfc_issues {where}
                ORDER BY COALESCE(sale_end, created_at) DESC, issue DESC
                LIMIT :lim OFFSET :off"""),
            {**params, "lim": size, "off": (page - 1) * size}).fetchall()]
        data = _load(db, issues)
    finally:
        db.close()

    return {"data": [data[i] for i in issues if i in data],
            "total": total, "page": page, "page_size": size}


def get_issue_review(issue: str) -> dict | None:
    _ensure_once()
    db = SessionLocal()
    try:
        return _load(db, [str(issue)]).get(str(issue))
    finally:
        db.close()


def latest_issue() -> str | None:
    db = SessionLocal()
    try:
        return db.execute(text(
            "SELECT issue FROM sfc_issues ORDER BY COALESCE(sale_end, created_at) DESC LIMIT 1"
        )).scalar()
    finally:
        db.close()
