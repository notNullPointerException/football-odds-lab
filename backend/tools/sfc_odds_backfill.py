# -*- coding: utf-8 -*-
"""把【竞彩历史开奖结果】里的胜平负赔率，回填到 sfc_issue_matches.had_*。

用法(cwd = backend):
  .venv/bin/python tools/sfc_odds_backfill.py fetch      # 只抓竞彩历史，落缓存(可反复跑,断了续)
  .venv/bin/python tools/sfc_odds_backfill.py backfill   # 按缓存回填 sfc_issue_matches
  .venv/bin/python tools/sfc_odds_backfill.py all        # fetch + backfill
  .venv/bin/python tools/sfc_odds_backfill.py report     # 只看覆盖率，不写库
  .venv/bin/python tools/sfc_odds_backfill.py merge      # 并行分片并回主缓存(可选，见下)
  --dry-run / --since=YYYY-MM 可用

【并行抓取】全量 129 个月串行要 ~20 分钟，按月切成 N 片各自跑：

  for i in 0 1 2 3 4 5; do
    .venv/bin/python tools/sfc_odds_backfill.py fetch --shard=${i}of6 &
  done; wait

  每个分片写自己的 tools/jc_history_cache.<i>of<n>.json，互不覆盖；
  load_cache() 会自动把分片并进来，所以**抓完不 merge 也能直接 backfill**。
  merge 只是把分片折回主文件并删掉分片文件（想让目录干净时跑）。

【为什么要这个脚本】
胜负彩自己的开奖历史通道(lottery/getHistoryPageListV1, gameNo=90)**不给赔率** ——
历史期 h/d/a 全是空字符串，连最新已开奖期都是。但竞彩另有一条通道有：

    gateway/uniform/football/getUniformMatchResultV1.qry     (回溯到 2014-12)
    ?matchBeginDate=&matchEndDate=&leagueId=&pageSize=&pageNo=&isFix=&matchPage=1&pcOrWap=1

🔴 前缀是 `gateway/uniform/` 不是 `gateway/jc/` —— jc/ 那个路径返回 403「禁止访问」。
   这个真实路径是从官网 /jc/zqsgkj/ 页面的 jc_sgkj_gz.js 里挖出来的。

两张表靠 **infohubMatchId == matchId** 关联(已在 26133 上验过同号段)。老期没有 infohub，
退回「日期 + 队名前缀」匹配。

🔴 用户口径(2026-09-29)：「就以你接口拉到为准」—— 所以按接口原值落库，不做时点校正。
   但要记住这条：竞彩通道给的是【收盘】赔率，而 sfc_archive_service 写进同一列的
   是【赛前实时】赔率。所以 had_* 一列现在混着两个时点，按来源区分：
   历史导入的行=收盘价，赛前存档的行(赛前存档过的那期)=实时价。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.services.sfc_archive_service import ensure_tables  # noqa: E402

_URL = ("https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry"
        "?matchBeginDate={b}&matchEndDate={e}&leagueId=&pageSize=100&pageNo={p}"
        "&isFix=0&matchPage=1&pcOrWap=1")

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jc_history_cache.json")

# 竞彩数据最早到 2014-12（2014-03 及更早 total=0）
_FIRST_YEAR, _FIRST_MONTH = 2014, 12


def _http_json(url: str, timeout: int = 25, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.sporttery.cn/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"拉取失败 {url}: {last}")


def _clean(s) -> str:
    """体彩对队名做视觉对齐填充（"瑞  士"），空格是排版不是名字。"""
    return re.sub(r"\s+", "", str(s or ""))


def _dec(v):
    s = str(v or "").strip()
    return s or None


def _months(since: str):
    """('2014-12' 起) 逐月产出 (年, 月)。"""
    y, m = _FIRST_YEAR, _FIRST_MONTH
    if since:
        y, m = int(since[:4]), int(since[5:7])
    import datetime
    today = datetime.date.today()
    while (y, m) <= (today.year, today.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _month_range(y: int, m: int):
    import calendar
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))

# 并行抓取时每个分片一个文件：jc_history_cache.<i>of<n>.json
_SHARD_RE = re.compile(r"^jc_history_cache\.(\d+)of(\d+)\.json$")


def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _shard_files() -> list:
    """并行分片缓存文件（按 i 排序）。没跑过并行时返回空表，单进程路径完全不变。"""
    try:
        names = os.listdir(_CACHE_DIR)
    except OSError:
        return []
    return [os.path.join(_CACHE_DIR, n) for n in sorted(names) if _SHARD_RE.match(n)]


def load_cache() -> dict:
    """主缓存 + 所有分片。

    分片只补【主缓存里没有的月份】（`not c.get(k)`），主缓存优先 ——
    所以并行抓完不必先 merge，backfill/report 直接就能看到全部数据。
    """
    c = _load(_CACHE)
    for p in _shard_files():
        for k, v in _load(p).items():
            if v and not c.get(k):
                c[k] = v
    return c


def save_cache(c: dict, path: str = _CACHE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False)
    os.replace(tmp, path)


def cmd_fetch(args):
    todo = [f"{y:04d}-{m:02d}" for y, m in _months(args.since)]
    path = _CACHE
    if args.shard:
        # 按月切分：各分片只认自己那几个月，写自己的文件 —— 互不覆盖，可同时跑。
        i, n = (int(x) for x in args.shard.split("of"))
        if not 0 <= i < n:
            raise SystemExit(f"--shard 要 iof 形式且 0 <= i < n，收到 {args.shard!r}")
        todo = [k for j, k in enumerate(todo) if j % n == i]
        path = os.path.join(_CACHE_DIR, f"jc_history_cache.{i}of{n}.json")
    cache = _load(path)
    # 🔴 跳过判据要看【主缓存 + 全部分片】，不能只看自己的文件 —— 否则已经抓过的月份
    #    会被分片重抓一遍（幂等但白费：51 个月的重复抓取 = 多花 60% 的时间）。
    have = load_cache()
    left = [k for k in todo if not have.get(k)]
    print(f"[{os.path.basename(path)}] 分到 {len(todo)} 个月, 已有 {len(todo)-len(left)}, "
          f"待抓 {len(left)}", flush=True)

    done = 0
    for key in left:
        y, m = int(key[:4]), int(key[5:7])
        b, e = _month_range(y, m)
        rows, page, pages = [], 1, 1
        ok = True
        try:
            while page <= pages:
                v = _http_json(_URL.format(b=b, e=e, p=page)).get("value") or {}
                pages = int(v.get("pages") or 1)
                for x in v.get("matchResult") or []:
                    mid = x.get("matchId")
                    if not mid:
                        continue
                    rows.append({
                        "id": mid,
                        "d": x.get("matchDate"),
                        "h": _clean(x.get("allHomeTeam")),
                        "a": _clean(x.get("allAwayTeam")),
                        "oh": _dec(x.get("h")), "od": _dec(x.get("d")), "oa": _dec(x.get("a")),
                        "line": _dec(x.get("goalLine")),
                        "num": _clean(x.get("matchNumStr")),
                        "sc": _dec(x.get("sectionsNo999")),
                    })
                page += 1
                if page <= pages:
                    time.sleep(args.sleep)
        except RuntimeError as ex:
            # 失败【不落缓存】—— 下次跑会自动重抓这个月。也【不中断】：
            # 一个月的网络抖动不该让整个分片停摆（老版本这里是 break，并行跑时特别吃亏）。
            print(f"  ! {key} 失败, 稍后重跑会补: {ex}", flush=True)
            ok = False
        if ok:
            cache[key] = rows
            save_cache(cache, path)
        done += 1
        if done % 5 == 0 or done == len(left):
            print(f"  [{done}/{len(left)}] {key}  {len(rows)} 场", flush=True)
        time.sleep(args.sleep)

    n_rows = sum(len(v) for v in cache.values())
    print(f"[{os.path.basename(path)}] 完成: {len(cache)} 个月 / {n_rows} 场 -> {path}", flush=True)


def cmd_merge(args):
    """把并行分片折回主缓存并删掉分片文件。

    **可选步骤** —— load_cache() 本来就会自动并分片。跑它只是让 tools/ 目录干净、
    并且让「主缓存」变成唯一的真相来源。月份冲突时主缓存优先（同 load_cache 语义）。
    """
    main = _load(_CACHE)
    files = _shard_files()
    moved = 0
    for p in files:
        for k, v in _load(p).items():
            if v and not main.get(k):
                main[k] = v
                moved += 1
        os.remove(p)
    save_cache(main)
    print(f"合并 {len(files)} 个分片 / 新增 {moved} 个月 -> {_CACHE}", flush=True)


def _date_span(begin, end) -> list:
    """[begin, end] 之间的自然日字符串，**末尾多给一天** —— 开奖时刻常写成次日 0 点
    （26133 是 2026-09-28 00:00），而比赛在前一晚。上限 15 天，防脏数据把窗口拉爆。
    列可能是 DATE 也可能是 VARCHAR(16)，两种都吃。
    """
    import datetime

    def _d(v):
        if v is None:
            return None
        if isinstance(v, datetime.datetime):
            return v.date()
        if isinstance(v, datetime.date):
            return v
        try:
            return datetime.date(*map(int, str(v).strip()[:10].split("-")))
        except (ValueError, TypeError):
            return None

    b, e = _d(begin), _d(end)
    if not b or not e or e < b:
        return []
    out, d = [], b
    while d <= e + datetime.timedelta(days=1) and len(out) < 15:
        out.append(d.strftime("%Y-%m-%d"))
        d += datetime.timedelta(days=1)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^一-龥A-Za-z]", "", str(s or ""))


def _pref_match(a: str, b: str) -> bool:
    """体彩在胜负彩里把队名截断（爱沙尼 vs 爱沙尼亚、法罗 vs 法罗群岛、
    斯洛文 vs 斯洛文尼亚），所以用前缀包含。至少 2 字，避免"中国"这种过短误配。"""
    a, b = _norm(a), _norm(b)
    if len(a) < 2 or len(b) < 2:
        return False
    return a.startswith(b) or b.startswith(a)


def cmd_backfill(args):
    ensure_tables()
    cache = load_cache()
    if not cache:
        print("缓存为空，先跑 fetch")
        return

    by_id, by_dt = {}, {}
    for key, rows in cache.items():
        for r in rows:
            by_id[r["id"]] = r
            if r.get("oh") and r.get("d"):
                by_dt.setdefault(r["d"], []).append(r)
    n_odds = sum(len(v) for v in by_dt.values())
    print(f"竞彩缓存: {len(by_id)} 场 (其中带赔率 {n_odds})")

    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT m.issue, m.no, m.home, m.away, m.match_date, m.infohub_match_id,
                   i.sale_begin, i.draw_time
            FROM sfc_issue_matches m LEFT JOIN sfc_issues i ON i.issue = m.issue
            ORDER BY m.issue, m.no""")).mappings().all()
        print(f"胜负彩行: {len(rows)}")

        stat = {"by_id": 0, "by_name": 0, "by_win": 0, "no": 0, "filled": 0}
        batch = []
        for r in rows:
            hit = None
            if r["infohub_match_id"]:
                hit = by_id.get(r["infohub_match_id"])
                if hit:
                    stat["by_id"] += 1
            has_name = bool(r["home"]) and bool(r["away"])
            if hit is None and r["match_date"] and has_name:
                cand = [x for x in by_dt.get(str(r["match_date"]), [])
                        if _pref_match(r["home"], x["h"]) and _pref_match(r["away"], x["a"])]
                if len(cand) == 1:
                    hit = cand[0]
                    stat["by_name"] += 1
            if hit is None and not r["match_date"] and has_name:
                # 🔴 上游 2020 年前大面积不给 startTime（2019 有 53% 的行没日期），
                #    没有日期锚点时任何队名匹配都无从谈起。退回【期次销售窗口 + 队名前缀】：
                #    窗口只有 4~7 天，且要求主客两队前缀都命中且【唯一】。
                #    同两队 7 天内再赛一次极少，但这是【启发式】不是精确关联，
                #    所以单独计数（by_win）—— 别和上面两档混着看。
                cand = []
                for d in _date_span(r["sale_begin"], r["draw_time"]):
                    cand += [x for x in by_dt.get(d, [])
                             if _pref_match(r["home"], x["h"]) and _pref_match(r["away"], x["a"])]
                if len(cand) == 1:
                    hit = cand[0]
                    stat["by_win"] += 1
            if not hit or not hit.get("oh"):
                stat["no"] += 1
                continue
            batch.append({"issue": r["issue"], "no": r["no"],
                          "h": hit["oh"], "d": hit["od"], "a": hit["oa"]})

        print(f"关联到: infohub 精确 {stat['by_id']}  日期+队名 {stat['by_name']}  "
              f"销售窗口兜底(启发式) {stat['by_win']}  没招 {stat['no']}   "
              f"合计可填 {len(batch)}")
        if args.dry_run:
            print("[dry-run] 不写库")
            return

        # 只填空白：had_h IS NULL 才写 —— 保护 sfc_archive_service 写的赛前实时赔率
        sql = text("""UPDATE sfc_issue_matches
            SET had_h=:h, had_d=:d, had_a=:a
            WHERE issue=:issue AND no=:no AND had_h IS NULL""")
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            res = db.execute(sql, chunk)
            stat["filled"] += res.rowcount or 0
            db.commit()
        print(f"已写入 {stat['filled']} 行（跳过 had_h 已有值的行）")
        _coverage(db)
    finally:
        db.close()


def _coverage(db):
    t = db.execute(text("""SELECT COUNT(*) n, SUM(had_h IS NOT NULL) had,
        SUM(mkt_h IS NOT NULL) mkt, SUM(infohub_match_id IS NOT NULL) ih
        FROM sfc_issue_matches""")).mappings().all()[0]
    # 🔴 MySQL 的 SUM() 回来是 Decimal，直接乘 100.0 会 TypeError，先 float()
    nn, had = float(t["n"] or 0), float(t["had"] or 0)
    print(f"\n覆盖率: sfc_issue_matches {int(nn)} 行  有体彩赔率 {int(had)} "
          f"({had*100.0/nn:.1f}%)   有庄家盘 {t['mkt']}   有 infohub {t['ih']}")
    print("  有赔率的按年:")
    for x in db.execute(text("""SELECT LEFT(issue,2) yy, COUNT(*) n,
        SUM(had_h IS NOT NULL) h FROM sfc_issue_matches GROUP BY yy ORDER BY yy""")).mappings().all():
        bar = "#" * int(x["h"] * 40 / max(x["n"], 1))
        print(f"    {x['yy']}xxxx {x['h']:>5}/{x['n']:<5} {bar}")


def cmd_report(args):
    ensure_tables()
    db = SessionLocal()
    try:
        _coverage(db)
    finally:
        db.close()


def cmd_all(args):
    cmd_fetch(args)
    cmd_backfill(args)


def main():
    ap = argparse.ArgumentParser(description="竞彩历史赔率 -> 胜负彩表")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("fetch", cmd_fetch), ("backfill", cmd_backfill),
                     ("report", cmd_report), ("merge", cmd_merge), ("all", cmd_all)):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--since", default="", help="从 YYYY-MM 起（默认 2014-12）")
        p.add_argument("--sleep", type=float, default=0.3, help="翻页间隔秒")
        p.add_argument("--shard", default="", help="并行分片 iofn 如 0of6（只有 fetch 用）")
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
