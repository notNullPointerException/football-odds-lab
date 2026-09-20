# -*- coding: utf-8 -*-
"""竞彩注单记账 + 周复盘服务（用户 2026-09-10 定稿的记录/复盘体系）

设计原则:
- 三张表 raw SQL 建表(不进 alembic, 与 predictions/scheduler_* 同思路):
    bet_journal 注单(可含多条腿) / bet_legs 串关腿 / bet_config 资金与纪律设置
- 赛前只写"依据"(赔率/模型概率/去水市场概率/Edge/触发信号), 赛后 settle 只回填结果字段,
  一律不修改赛前依据 —— 防止事后美化记忆
- 复盘只按记录算指标, 不凭记忆: 分玩法命中率 / ROI / Edge 校准表 / 分信号 / 纪律检查
"""
from datetime import datetime, timedelta

from sqlalchemy import text
from loguru import logger

from app.db.session import SessionLocal

MARKETS = ("1x2", "handicap", "ou")

_DDL = [
    """CREATE TABLE IF NOT EXISTS bet_journal (
        id INT AUTO_INCREMENT PRIMARY KEY,
        bet_date DATE NOT NULL COMMENT '下注日期(北京时间)',
        batch VARCHAR(32) NULL COMMENT '批次标签, 如 9-9周三',
        ticket_type VARCHAR(16) NOT NULL COMMENT '单关/2串1/3串1/...',
        is_main TINYINT NOT NULL DEFAULT 0 COMMENT '是否主推(用户实盘口径)',
        total_odds DECIMAL(10,3) NOT NULL,
        stake DECIMAL(10,2) NULL COMMENT '金额, 未知可留空',
        status VARCHAR(12) NOT NULL DEFAULT 'pending' COMMENT 'pending/won/lost/void',
        payout DECIMAL(10,2) NULL,
        pnl DECIMAL(10,2) NULL,
        note VARCHAR(255) NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        settled_at DATETIME NULL,
        KEY idx_date (bet_date),
        KEY idx_main (is_main)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS bet_legs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ticket_id INT NOT NULL,
        fixture_id BIGINT NULL COMMENT '关联 fixtures.id',
        jc_no VARCHAR(16) NULL COMMENT '竞彩编号',
        match_label VARCHAR(128) NOT NULL,
        market VARCHAR(16) NOT NULL COMMENT '1x2/handicap/ou',
        selection VARCHAR(32) NOT NULL COMMENT '主胜/让平/大球...',
        line_val DECIMAL(5,2) NULL COMMENT '让球线(主队视角)/大小球线',
        odds DECIMAL(10,3) NOT NULL,
        model_prob DECIMAL(6,4) NULL COMMENT '模型概率',
        market_prob DECIMAL(6,4) NULL COMMENT '去水市场概率',
        edge DECIMAL(6,4) NULL COMMENT '模型-市场(pp)',
        signal_tag VARCHAR(128) NULL COMMENT '触发信号',
        leg_result VARCHAR(8) NULL COMMENT 'win/lose/push',
        actual_score VARCHAR(16) NULL,
        KEY idx_ticket (ticket_id),
        KEY idx_fixture (fixture_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS bet_config (
        k VARCHAR(32) PRIMARY KEY,
        v VARCHAR(64) NOT NULL,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
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


# ──── 写入 ────

def log_ticket(payload: dict, db=None) -> int:
    """记录一张注单(含串关腿)。payload 见 tools/bet_journal.py 的 JSON 结构。"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        legs = payload.get("legs") or []
        if not legs:
            raise ValueError("注单至少要有一条腿")
        if "total_odds" not in payload:
            tot = 1.0
            for lg in legs:
                tot *= float(lg["odds"])
            payload["total_odds"] = round(tot, 3)
        res = db.execute(
            text("""INSERT INTO bet_journal
                (bet_date, batch, ticket_type, is_main, total_odds, stake, note)
                VALUES (:bet_date, :batch, :ticket_type, :is_main, :total_odds, :stake, :note)"""),
            {
                "bet_date": payload["bet_date"],
                "batch": payload.get("batch"),
                "ticket_type": payload.get("ticket_type", "单关"),
                "is_main": 1 if payload.get("is_main") else 0,
                "total_odds": payload["total_odds"],
                "stake": payload.get("stake"),
                "note": payload.get("note"),
            },
        )
        ticket_id = res.lastrowid
        for lg in legs:
            mp = lg.get("model_prob")
            mkp = lg.get("market_prob")
            edge = (mp - mkp) if (mp is not None and mkp is not None) else None
            db.execute(
                text("""INSERT INTO bet_legs
                    (ticket_id, fixture_id, jc_no, match_label, market, selection, line_val,
                     odds, model_prob, market_prob, edge, signal_tag)
                    VALUES (:tid, :fid, :jc_no, :label, :market, :sel, :line,
                            :odds, :mp, :mkp, :edge, :tag)"""),
                {
                    "tid": ticket_id, "fid": lg.get("fixture_id"), "jc_no": lg.get("jc_no"),
                    "label": lg["match_label"], "market": lg["market"], "sel": lg["selection"],
                    "line": lg.get("line_val"), "odds": lg["odds"],
                    "mp": mp, "mkp": mkp, "edge": edge, "tag": lg.get("signal_tag"),
                },
            )
        db.commit()
        return ticket_id
    finally:
        if own:
            db.close()


# ──── 赛后结算(只回填结果字段) ────

def _leg_result(market: str, selection: str, line_val, hg: int, ag: int) -> str:
    m = hg - ag
    total = hg + ag
    if market == "1x2":
        actual = "主胜" if m > 0 else ("平" if m == 0 else "客胜")
        return "win" if selection == actual else "lose"
    if market == "handicap":
        t = -float(line_val)  # 让球线转"目标净胜": -1 -> 净胜1=让平
        actual = "让胜" if m > t else ("让平" if m == t else "让负")
        return "win" if selection == actual else "lose"
    if market == "ou":
        if total > float(line_val):
            actual = "大球"
        elif total < float(line_val):
            actual = "小球"
        else:
            return "push"
        return "win" if selection == actual else "lose"
    raise ValueError(f"未知玩法 {market}")


def settle_bets(db=None) -> dict:
    """按 fixtures 已完赛结果回填 bet_legs.leg_result, 并重算注单 status/payout/pnl"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        legs = db.execute(text("""SELECT id, ticket_id, fixture_id, market, selection, line_val
            FROM bet_legs WHERE leg_result IS NULL AND fixture_id IS NOT NULL""")).mappings().all()
        updated = 0
        for lg in legs:
            fx = db.execute(text("""SELECT goals_home, goals_away, status_short FROM fixtures
                WHERE id = :fid"""), {"fid": lg["fixture_id"]}).mappings().first()
            if not fx or fx["goals_home"] is None:
                continue
            if fx["status_short"] not in ("FT", "AET", "PEN"):
                continue
            hg, ag = int(fx["goals_home"]), int(fx["goals_away"])
            r = _leg_result(lg["market"], lg["selection"], lg["line_val"], hg, ag)
            db.execute(text("""UPDATE bet_legs SET leg_result=:r, actual_score=:s WHERE id=:id"""),
                       {"r": r, "s": f"{hg}-{ag}", "id": lg["id"]})
            updated += 1

        tickets = db.execute(text("""SELECT id, stake, total_odds FROM bet_journal
            WHERE status='pending'""")).mappings().all()
        settled = 0
        for tk in tickets:
            lr = [r["leg_result"] for r in db.execute(text("""SELECT leg_result FROM bet_legs
                WHERE ticket_id=:tid"""), {"tid": tk["id"]}).mappings().all()]
            if any(x is None for x in lr):
                continue
            status = "won" if all(x in ("win", "push") for x in lr) else "lost"
            payout = None
            pnl = None
            if tk["stake"] is not None:
                stake = float(tk["stake"])
                if status == "won":
                    # 逐腿连乘赔率；push 腿按 1.0 计（不贡献赔率，也不判负）
                    # ⚠️ 必须按列名取值：.mappings() 给的是 RowMapping，
                    #    `for x, o in row_mapping` 解出来的是"键名"而非"列值"，
                    #    x == "win" 会恒假 → eff 恒为 1.0 → 中奖票 pnl 记成 0。
                    #    （2026-09-19 #8 是首张 win 票，此 bug 才暴露）
                    eff = 1.0
                    for row in db.execute(text("""SELECT leg_result, odds FROM bet_legs
                        WHERE ticket_id=:tid"""), {"tid": tk["id"]}).mappings().all():
                        if row["leg_result"] == "win":
                            eff *= float(row["odds"])
                    payout = round(stake * eff, 2)
                    pnl = round(payout - stake, 2)
                else:
                    payout = 0.0
                    pnl = round(-stake, 2)
            db.execute(text("""UPDATE bet_journal SET status=:st, payout=:po, pnl=:pn,
                settled_at=:now WHERE id=:id"""),
                       {"st": status, "po": payout, "pn": pnl, "now": datetime.now(), "id": tk["id"]})
            settled += 1
        db.commit()
        return {"legs_updated": updated, "tickets_settled": settled}
    finally:
        if own:
            db.close()


# ──── 周复盘 ────

def _week_range(week: str | None) -> tuple:
    """返回 (周一 00:00, 下周一 00:00) 的 date; week: this/last/YYYY-Www/None"""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    if week in (None, "this"):
        start = monday
    elif week == "last":
        start = monday - timedelta(days=7)
    elif "-W" in week:
        y, w = week.split("-W")
        start = datetime.fromisocalendar(int(y), int(w), 1).date()
    else:  # 直接给周一日期
        start = datetime.strptime(week, "%Y-%m-%d").date()
    return start, start + timedelta(days=7)


def weekly_report(week: str | None = None, db=None) -> str:
    """生成周复盘 markdown(只按记录算, 五个指标)。week 默认本期。"""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        start, end = _week_range(week)
        rows = db.execute(text("""SELECT * FROM bet_journal
            WHERE bet_date >= :s AND bet_date < :e ORDER BY bet_date, id"""),
                          {"s": start, "e": end}).mappings().all()
        tids = [r["id"] for r in rows]
        legs = []
        if tids:
            legs = db.execute(text("""SELECT * FROM bet_legs WHERE ticket_id IN :tids"""),
                              {"tids": tuple(tids)}).mappings().all()

        L = []
        iso = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
        L.append(f"# 竞彩周复盘 {iso}（{start} ~ {end - timedelta(days=1)}）")
        L.append(f"生成时间 {datetime.now():%Y-%m-%d %H:%M}｜注单 {len(rows)} 张、腿 {len(legs)} 条")

        settled_t = [r for r in rows if r["status"] in ("won", "lost")]
        staked = sum(float(r["stake"]) for r in settled_t if r["stake"] is not None)
        pnl = sum(float(r["pnl"]) for r in settled_t if r["pnl"] is not None)
        L.append("\n## 1. 总览")
        if staked:
            L.append(f"- 已结算 {len(settled_t)}/{len(rows)} 张；投入 ¥{staked:.0f}，盈亏 **¥{pnl:+.0f}**"
                     f"，ROI **{pnl / staked * 100:+.1f}%**")
        else:
            L.append(f"- 已结算 {len(settled_t)}/{len(rows)} 张；注单金额未回填 → ROI 待算"
                     f"（`bet_journal.stake` 填上后自动出 ROI）")
        wins = sum(1 for r in settled_t if r["status"] == "won")
        if settled_t:
            L.append(f"- 注单命中 {wins}/{len(settled_t)} = {wins / len(settled_t) * 100:.0f}%")

        # 2. 分玩法命中率(腿级)
        L.append("\n## 2. 分玩法命中率（腿级）")
        L.append("| 玩法 | 腿数 | 命中 | 命中率 |")
        L.append("|---|---|---|---|")
        for mk, name in (("1x2", "胜平负"), ("handicap", "让球"), ("ou", "大小球")):
            sub = [g for g in legs if g["market"] == mk and g["leg_result"] in ("win", "lose")]
            if not sub:
                continue
            w = sum(1 for g in sub if g["leg_result"] == "win")
            L.append(f"| {name} | {len(sub)} | {w} | {w / len(sub) * 100:.0f}% |")
        main_legs = [g for g in legs if g["ticket_id"] in {r["id"] for r in rows if r["is_main"]}]
        ml = [g for g in main_legs if g["leg_result"] in ("win", "lose")]
        if ml:
            mw = sum(1 for g in ml if g["leg_result"] == "win")
            L.append(f"\n主推腿命中 {mw}/{len(ml)} = {mw / len(ml) * 100:.0f}%")

        # 3. Edge 校准表
        L.append("\n## 3. Edge 校准表（模型概率 − 去水市场概率，最理想是越高越准）")
        L.append("| Edge 档 | 腿数 | 命中率 |")
        L.append("|---|---|---|")
        buckets = [("≤0（负 Edge）", -9, 0), ("0~5%", 0, 0.05), ("5~10%", 0.05, 0.10), (">10%", 0.10, 9)]
        for name, lo, hi in buckets:
            sub = [g for g in legs if g["edge"] is not None and lo <= float(g["edge"]) < hi
                   and g["leg_result"] in ("win", "lose")]
            if not sub:
                continue
            w = sum(1 for g in sub if g["leg_result"] == "win")
            L.append(f"| {name} | {len(sub)} | {w / len(sub) * 100:.0f}% |")

        # 4. 分信号复盘
        L.append("\n## 4. 分信号复盘（按触发信号）")
        tags = {}
        for g in legs:
            if g["leg_result"] not in ("win", "lose"):
                continue
            tags.setdefault(g["signal_tag"] or "(未标)", []).append(g["leg_result"])
        if tags:
            L.append("| 信号 | 腿数 | 命中率 |")
            L.append("|---|---|---|")
            for t, v in sorted(tags.items(), key=lambda kv: -len(kv[1])):
                L.append(f"| {t} | {len(v)} | {sum(1 for x in v if x == 'win') / len(v) * 100:.0f}% |")

        # 5. 纪律检查
        L.append("\n## 5. 纪律检查")
        stakes = [float(r["stake"]) for r in rows if r["stake"] is not None]
        if stakes:
            L.append(f"- 单注金额：最低 ¥{min(stakes):.0f}／平均 ¥{sum(stakes) / len(stakes):.0f}／最高 ¥{max(stakes):.0f}")
        neg = [g for g in legs if g["edge"] is not None and float(g["edge"]) <= 0]
        L.append(f"- 负 Edge 腿（冲动单嫌疑）：{len(neg)} 条{'：' + '、'.join(x['match_label'] for x in neg[:5]) if neg else ''}")
        pend = [r for r in rows if r["status"] == "pending"]
        L.append(f"- 待结算：{len(pend)} 张")
        # 累计回撤(全量, 按日)
        allp = db.execute(text("""SELECT bet_date, SUM(pnl) p FROM bet_journal
            WHERE pnl IS NOT NULL GROUP BY bet_date ORDER BY bet_date""")).mappings().all()
        if allp:
            cum, peak, mdd = 0.0, 0.0, 0.0
            for r in allp:
                cum += float(r["p"])
                peak = max(peak, cum)
                mdd = min(mdd, cum - peak)
            cfg = _config_get(db, "bankroll")
            L.append(f"- 累计盈亏 ¥{cum:+.0f}（峰值 ¥{peak:+.0f}，最大回撤 ¥{mdd:.0f}）"
                     + (f"｜当前资金设置 ¥{cfg}" if cfg else "｜（bet_config.bankroll 未设置）"))
        else:
            L.append("- 累计盈亏/最大回撤：暂无已回填金额的注单")
        unit = _config_get(db, "unit_pct") or "1"
        L.append(f"- 注额纪律：平注建议 = 资金 × {unit}%；进阶 1/4 Kelly = Edge ÷ 赔率 ÷ 4")

        # 6. 主推专线
        main_t = [r for r in rows if r["is_main"]]
        if main_t:
            L.append("\n## 6. 主推专线（用户实盘口径）")
            for r in main_t:
                lg = [g for g in legs if g["ticket_id"] == r["id"]]
                desc = " × ".join(f"{g['jc_no'] or ''}{g['match_label']} {g['selection']}@{g['odds']}" for g in lg)
                L.append(f"- {r['bet_date']} {r['ticket_type']} @{r['total_odds']} "
                         f"[{r['status']}] {desc}" + (f"　盈亏 ¥{float(r['pnl']):+.0f}" if r["pnl"] is not None else ""))

        L.append("\n## 7. 行动建议（按上表读数）")
        L.append("- 某玩法 ROI/命中率长期为负 → 该玩法停投；Edge 高档命中率低于低档 → 该档模型系统性错误，只做反向参考或剔除")
        L.append("- 连续 3 个月 ROI 为负 → 停实盘，回炉模型（保护本金优先）")
        return "\n".join(L)
    finally:
        if own:
            db.close()


def save_report(md: str) -> str:
    import os
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "reports")
    os.makedirs(out_dir, exist_ok=True)
    name = f"bet_review_{datetime.now():%Y-%m-%d}.md"
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def _config_get(db, key: str):
    row = db.execute(text("SELECT v FROM bet_config WHERE k=:k"), {"k": key}).first()
    return row[0] if row else None


def set_config(key: str, value: str):
    db = SessionLocal()
    try:
        db.execute(text("""INSERT INTO bet_config (k, v) VALUES (:k, :v)
            ON DUPLICATE KEY UPDATE v=:v"""), {"k": key, "v": str(value)})
        db.commit()
    finally:
        db.close()


# ──── 定时任务入口(每周一 09:06 由 scheduler 触发; 非周一直接跳过) ────

def weekly_review_job(db=None):
    if datetime.now().weekday() != 0:
        return
    md = weekly_report("last", db=db)
    path = save_report(md)
    logger.info(f"[竞彩周复盘] 已生成 {path}")
