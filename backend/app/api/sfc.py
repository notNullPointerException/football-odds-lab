# -*- coding: utf-8 -*-
"""胜负彩（传统足彩「14 场胜负游戏」）实时数据 —— 只读代理。

为什么单开一个端点：胜负彩和竞彩是【两个彩种】，接口 / 玩法 / 结构全不同。
  · 竞彩   getMatchCalculatorV1.qry   玩法 had（胜平负）+ hhad（让球胜平负），一次给 3 个销售日
  · 胜负彩 getFootBallMatchV1.qry     玩法【只有胜平负】，一期固定 14 场，按【期号】卖

⚠️ 胜负彩接口【只给比赛日期、不给开球时间】（字段 startTime 形如 "2026-09-19"）。
竞彩的 matchId 与胜负彩的 infohubMatchId 是同一个 id，所以用它把开球时间关联过来。
关联不上的场次（那一场竞彩当期没开售）就只显示日期 —— 这是数据源本身的缺口，
不是 bug，也不能靠队名猜（胜负彩写「布里斯」而竞彩写「布城」，猜不准）。
"""
import asyncio
import json
import re
import time

from fastapi import APIRouter, HTTPException
from loguru import logger
from sqlalchemy import text

# 去水公式(_parse_pool→_devig)、HTTP 打点、竞彩 URL 都和竞彩页共用同一份，
# 避免两个页面各算一套数字；fixture_id 也共用同一个解析 service，
# 保证同一场比赛在两个页面上显示出来的 id 永远一致。
from app.api.jc import _JC_URL, _http_json, _parse_pool
from app.db.session import SessionLocal
from app.services import jc_match_service, sfc_archive_service, sfc_check_service

router = APIRouter()

_SFC_URL = ("https://webapi.sporttery.cn/gateway/lottery/getFootBallMatchV1.qry"
            "?param=90,0&isFix=0&pcOrWap=1")

# 15s 内存缓存，防手抖连点打爆体彩接口（与竞彩页同口径）
_CACHE_TTL = 15.0
_cache: dict = {"at": 0.0, "payload": None}

# 存档防抖：上游【只返回当前期】，翻期之后这 14 场就永远取不回来了，所以每次拿到
# payload 都得顺手落库。但页面来回切 tab 会反复命中同一次 fetch，14 行写一遍够了 ——
# 120 秒内同一期只写一次（手动存档会传 force=True 绕过）。
_ARCHIVE_TTL = 120.0
_archived: dict = {"issue": None, "at": 0.0}

# 胜负彩的彩果记法：3=主胜 1=平 0=客胜
_RESULT_LABEL = {"3": "胜", "1": "平", "0": "负"}

# T（本期总票数）的来源说明 —— 开奖后是本期真值，开奖前只能拿近 5 期中位销量估。
# 标注出来是为了让「T = 销量/2」这句话在两种情况下都能自己解释清楚。
_T_LABEL = {"own": "本期销量/2（已开奖，真值）",
            "median5": "近 5 期中位销量推算的总票数"}

# P（每场去水概率）用的是哪套盘 —— 必须先体彩，体彩不齐才整体回退庄家。
# 🔴 两套盘的去水概率【不等】（devig 是「方法, 公司」配对问题），所以来源必须露在
#    报告面上：读者要能一眼看出这份报告跟别期不是一个口径。
_ODDS_SRC = {"jc": "体彩竞彩", "pinnacle": "庄家Pinnacle(体彩不齐,整体回退)"}


def _name(s) -> str:
    """体彩对两字队名做【视觉对齐填充】（"拜  仁"、"蒙  扎"），中间空格是排版不是名字。"""
    return re.sub(r"\s+", "", s or "")


def _int(v):
    """安全转 int。

    实测体彩的 matchId 正常情况下是数字，但不保证；直接 int() 的话，
    一个非数字 id 会抛 ValueError 一路冒到 _build_payload，
    结果是【整页 502 / 退回旧缓存】—— 一个坏字段炸掉一整页。
    转不动就当没有这个 id，降级成本只是这一场少个开球时间而已。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _had(h, d, a):
    """胜负彩的赔率是平铺的 h/d/a，不像竞彩那样包在 pool 里。

    三个都空 = 该场没开赔率（实测 14 场里有 5 场是这样）→ 返回 None，
    前端据此显示「未开」。走 _parse_pool 是为了让输出结构与竞彩页完全一致，
    这样前端 oddsCells() 一行都不用改。
    """
    if all(str(v or "").strip() == "" for v in (h, d, a)):
        return None
    return _parse_pool({"h": h, "d": d, "a": a})


def _jc_link_map(sfc_matches: list) -> list:
    """返回一个与 sfc_matches **按下标一一对应**的 entry 列表（len 恒等于 len(sfc_matches)）。

    entry = {"kickoff": "YYYY-MM-DD HH:MM:SS"|None, "fixture_id": int|None, "fixture_match": str}

    🔴 为什么是【按序的列表】而不是 `{matchId: entry}` 这种字典：
    胜负彩的 `infohubMatchId` 是【外部给的、不受我们约束】的字段，实测既有 0 值
    （多场共用），也可能重复。字典按 mid 索引时，两场共用一个 mid 会落到**同一个
    entry 对象**上 —— 第二场看到「已经有 fixture_id 了」就跳过自己的解析，
    **直接继承另一场比赛的 id**，页面、可点链接、庄家盘全跟着贴错，而且零日志。
    按序返回让每一场拿到**自己那份** dict，从根上不存在共享。别改回字典。

    两条路填 id：
      · ① 竞彩关联 —— 胜负彩的 infohubMatchId 就是竞彩的 matchId，靠它把
        【开球时间】和 fixture_id 一起关联过来（胜负彩接口自己只给日期）。
        走和竞彩页【完全同一个】jc_match_service，连 overrides 都是同一个文件，
        所以同一场比赛两个页面显示出来的 id 必然一致。
      · ② 胜负彩日期窗兜底 —— 竞彩当期**压根没开售**的那几场（实测 26134 期 14 场里
        有 8 场），① 永远给不出 id。这些走 resolve_by_day（理由里写「当日」以示区别）。

    ⚠️ ② **只在 ① 失败时才用，永不覆盖 ①** —— 日期窗有 150+ 个候选，是①的几百倍宽，
    闸门全靠 MIN_SCORE + MARGIN 两道；能走窄窗就必须走窄窗。

    单独 try + 更短超时：这些都是【附加信息】，拉不到就退化成只显示日期 / 编不出 id，
    绝不能因为关联失败把整个胜负彩页拖挂或让它超时翻倍。
    """
    entries = [{"kickoff": None, "fixture_id": None, "fixture_match": None}
               for _ in sfc_matches]
    db = None

    try:
        db = SessionLocal()

        # ---- ① 竞彩关联 ----
        days = []
        try:
            data = _http_json(_JC_URL, timeout=8)
            days = (data.get("value") or {}).get("matchInfoList") or []
        except Exception as e:
            logger.warning(f"胜负彩关联竞彩失败(降级为仅日期/无 fixture_id): {type(e).__name__}: {e}")

        # 🔴 resolve_batch 读的是【规范化字段】(home / away / kickoff / match_num)，
        # 而 subMatchList 是体彩原始结构(homeTeamAbbName / matchDate+matchTime / matchNumStr)——
        # 把原始 dict 直接传进去，四个字段一个都取不到，会把每场都【静默】判成「无从匹配」。
        # 症状很有欺骗性：kickoff 照常解析出来(那是上面直接拼的)，fixture_id 却全是 None，
        # 理由还写着「体彩没给开球时间」—— 自相矛盾，但不会报错，所以一直没人发现。
        # jc.py 是先建规范化副本再传，这里必须同样处理，否则两个页面的 id 对不上。
        resolved: dict = {}
        for day in days:
            raw_list = day.get("subMatchList") or []
            norm = [{
                "home": m.get("homeTeamAbbName", ""),
                "away": m.get("awayTeamAbbName", ""),
                # 与 jc.py 保持同源：队名判不出时靠这两个码兜底，缺了会让两个
                # 页面对同一场给出不同的 fixture_id（都写 overrides 又要写两遍）
                "home_code": m.get("homeTeamCode") or "",
                "away_code": m.get("awayTeamCode") or "",
                "kickoff": f"{m.get('matchDate', '')} {m.get('matchTime', '')}".strip(),
                "match_num": m.get("matchNumStr"),
            } for m in raw_list]
            jc_match_service.resolve_batch(db, norm, day.get("businessDate"))
            for m, n in zip(raw_list, norm):
                resolved[_int(m.get("matchId"))] = n

        jc_by_mid: dict = {}
        for day in days:
            for m in day.get("subMatchList") or []:
                mid = _int(m.get("matchId"))
                if mid is None:
                    continue
                d, t = m.get("matchDate"), m.get("matchTime")
                n = resolved.get(mid) or {}
                jc_by_mid[mid] = {
                    # 没日期就是 None，别拼出一个只有时间的假值
                    "kickoff": f"{d} {t}".strip() if d else None,
                    "fixture_id": n.get("fixture_id"),
                    "fixture_match": n.get("fixture_match"),
                }

        # ---- ② 逐场写进【自己那一行】的 entry ----
        # 🔴 这里是 entries[i] 按行赋值，不是往 mid 字典里塞对象。竞彩那份是只读来源，
        #    逐字段拷过来，两行共用同一 mid 时也只是各自拿到一份副本 —— 上面那种
        #    「第二场继承第一场 fixture_id」在结构上不可能再发生。
        ov = jc_match_service.load_overrides()
        for i, m in enumerate(sfc_matches):
            e = entries[i]
            mid = _int(m.get("infohubMatchId"))
            # mid 为 0/None 时【不能拿它当 key】—— 实测有 infohubMatchId=0 的场次，
            # 拿它去查只会撞上一场无关的比赛。
            hit = jc_by_mid.get(mid) if mid else None
            if hit:
                e["kickoff"] = hit.get("kickoff")
                e["fixture_id"] = hit.get("fixture_id")
                e["fixture_match"] = hit.get("fixture_match")
            # ① 给不出 id 才走 ②（竞彩当期没开售的那几场）
            if not e.get("fixture_id"):
                r = jc_match_service.resolve_by_day(
                    db, (m.get("masterTeamAllName") or "").strip(),
                    (m.get("guestTeamAllName") or "").strip(),
                    m.get("startTime"), num=str(m.get("matchNum")), overrides=ov)
                e["fixture_id"] = r["fixture_id"]
                e["fixture_match"] = r["how"]
    except Exception as e:
        logger.warning(f"胜负彩解析 fixture_id 失败(降级为无 id): {type(e).__name__}: {e}")
    finally:
        if db is not None:
            db.close()

    return entries


def _pred_rows(fixture_ids: list) -> dict:
    """fixture_id -> {"book": 庄家盘, "pred": 模型头, "llm": LLM 头}。

    三块都出自 **predictions 表同一行**，字段名刻意和 `/api/predictions` 的
    `market` / `xgb` / `llm` 三个块保持一致 —— 前端因此能用**同一套渲染函数**
    画「首屏读到的」和「刚跑完写回的」两种来源，不会出现两处数字对不上。

      · book —— 庄家盘（Pinnacle 优先），给「体彩 vs 庄家」对照
      · pred —— 模型头：prob(1x2) / lambda / top3 / handicap / model_group
      · llm  —— LLM 头：方向、信心、比分

    `pred` 为 None = 这场**还没跑过预测**（页面显示「未跑」，这是要让人看见的状态，
    不是错误）；`llm` 单独给，是因为 L 头可能整块为空而不影响 prob 头有值。

    为什么胜负彩页要显示庄家盘：胜负彩自己的 `h/d/a` 只有**体彩开售了的那几场**才有
    （实测 26134 期 14 场里 8 场为空），而庄家盘 14/14 场都有。两个去水概率一相减
    才是「这场体彩给的水有多少」—— 这正是拿这页挑场次要看的数字。

    （predictions 的主键就是 fixture_id，一表一场，不用去重也不用担心互相覆盖。）

    ⚠️ 两边**拉取时刻不同**（体彩实时浮动，庄家盘是预测那一刻的快照），
    差 1~2pp 是噪声不是信号。

    ⚠️ `snapshot` 是【查 /odds 时用的那个日期参数】，也就是**开球的 UTC 日期**，
    **不是抓取时刻**（实测 1528909 的 snapshot="2026-09-29"，而它其实是 9-28 抓的）。
    前端别把它当「这是哪天的价」显示 —— 想要抓取时刻得另存字段，现在没有。

    整体 try + 失败兜底：这是**附加列**，DB 出问题就整列显示「–」，
    绝不能因为它把胜负彩页拖挂 —— 页面的主体是体彩实时数据。
    """
    out: dict = {}
    ids = [i for i in dict.fromkeys(fixture_ids) if i]
    if not ids:
        return out
    db = None
    try:
        db = SessionLocal()
        # 手拼占位符而不是 `IN :ids`：text() 里的元组参数需要 bindparam(expanding=True)
        # 才会展开，漏了它只会当成一个值去比，静默返回 0 行。
        ph = ",".join(f":f{i}" for i in range(len(ids)))
        rows = db.execute(text(f"""
            SELECT fixture_id, odds_home, odds_draw, odds_away, odds_meta,
                   model_group, win_home, win_draw, win_away,
                   lambda_home, lambda_away, top3_scores, handicap,
                   llm_win, llm_win_pct, llm_score,
                   llm_injuries, llm_lineups, updated_at
            FROM predictions WHERE fixture_id IN ({ph})
        """), {f"f{i}": v for i, v in enumerate(ids)}).mappings().all()

        for r in rows:
            # 逐行 try：某一行有脏数据只让它自己降级，不要连坐 —— 外层那个 try 一旦
            # 被触发，14 场里已经填好的都会跟着一起丢掉（附加列不该有这种杀伤力）。
            try:
                out[r["fixture_id"]] = _one_row(r)
            except Exception as e:
                logger.warning(f"胜负彩取预测行 fixture={r['fixture_id']} 失败: "
                               f"{type(e).__name__}: {e}")
    except Exception as e:
        logger.warning(f"胜负彩挂预测/庄家盘失败(降级为不显示): {type(e).__name__}: {e}")
    finally:
        if db is not None:
            db.close()
    return out


def _fmt_at(v):
    """updated_at -> "09-28 16:28"。datetime 之外的类型（字符串 / None / 数字）
    一律不抛异常：这一格只是个提示，不值得为它丢掉整场的预测数据。"""
    if v is None:
        return None
    try:
        return v.strftime("%m-%d %H:%M")
    except AttributeError:
        s = str(v)
        return s[5:16] if len(s) >= 16 else (s or None)


def _one_row(r) -> dict:
    """把 predictions 的一行拆成 book / pred / llm 三块（字段名对齐 /api/predictions）。"""
    entry: dict = {"book": None, "pred": None, "llm": None}

    meta = r["odds_meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    meta = meta or {}
    if r["odds_home"] is not None or meta:
        entry["book"] = {
            "bookmaker": meta.get("bookmaker"),
            "snapshot": meta.get("snapshot"),
            "bet": meta.get("bet"),
            "line": meta.get("line"),
            "h": r["odds_home"], "d": r["odds_draw"], "a": r["odds_away"],
            "payback": meta.get("payback"),
            "devig": meta.get("devig"),
        }

    # prob 三个都为 None = 这行只有赔率没有模型（极端情况），照样当「没预测」，
    # 免得前端拿三个 null 去算 argmax —— max() 会挑出第一个，显示成「主胜」。
    if r["win_home"] is not None or r["win_draw"] is not None or r["win_away"] is not None:
        top3 = r["top3_scores"]
        if isinstance(top3, str):
            try:
                top3 = json.loads(top3)
            except (ValueError, TypeError):
                top3 = None
        entry["pred"] = {
            "model_group": r["model_group"],
            "prob": {"home": r["win_home"], "draw": r["win_draw"],
                     "away": r["win_away"]},
            "lambda": ({"home": r["lambda_home"], "away": r["lambda_away"]}
                       if r["lambda_home"] is not None else None),
            "top3": top3 if isinstance(top3, list) else None,
            "handicap": r["handicap"],
            # 预测时刻，给人看「这是什么时候跑的」；created_at 是首次预测，
            # 反而看不出重跑过没有，所以这里用 updated_at。
            # 类型不保证（DB 改了列型就会变字符串）→ 转不动就留 None，别让它抛：
            # 抛出去是被逐行 try 接住，那一整场连庄家盘一起没了。
            "at": _fmt_at(r["updated_at"]),
        }
    # 伤停/首发跟 llm 那三个输出不是一回事：它是喂进 prompt 的实时情报，LLM 跑挂了也照样
    # 可能已经拉到。所以不塞进 `if r["llm_win"]` 里 —— 那正好会把「LLM 失败但伤停有数据」
    # 这种最该让人看见的情况吃掉。老行两列都是 NULL，条件为假，与改动前逐字段一致。
    extras = {"injuries": r["llm_injuries"], "lineups": r["llm_lineups"]}
    if r["llm_win"] or any(extras.values()):
        entry["llm"] = {"win": r["llm_win"], "win_pct": r["llm_win_pct"],
                        "score": r["llm_score"], **extras}

    return entry


def _build_payload() -> dict:
    data = _http_json(_SFC_URL)
    if data.get("success") is False:
        raise RuntimeError(f"体彩返回错误 {data.get('errorCode')}: {data.get('errorMessage')}")

    val = data.get("value") or {}
    s = val.get("sfcMatch") or {}
    # ⚠️ matchList 为空是【合法状态】（本期已停售 / 已开奖 / 未开售），不是错误。
    # 和竞彩页同一个教训：空 ≠ 拉取失败 —— 不能抛错弹红字，也不能拿旧一期冒充。
    matches_raw = s.get("matchList") or []

    # 与 matches_raw 按下标一一对应，**不要**在这里再按 mid / 场次号去查表：
    # 那等于把「一场的比赛信息可能来自另一场」重新引进来（见 _jc_link_map 的说明）。
    infos = _jc_link_map(matches_raw)

    # id 先全部定下来，再一次性把三块附加数据取回来 —— 别在下面的循环里逐场查，
    # 那是 14 次往返。
    recs = _pred_rows([i.get("fixture_id") for i in infos if i])

    matches = []
    for m, info in zip(matches_raw, infos):
        res = (m.get("result") or "").strip()
        fid = info.get("fixture_id") if info else None
        rec = recs.get(fid) or {}
        matches.append({
            "no": m.get("matchNum"),
            "league": (m.get("matchName") or "").strip(),
            "home": _name(m.get("masterTeamName")),
            "away": _name(m.get("guestTeamName")),
            "match_date": m.get("startTime") or "",
            # 关联上才是完整时间；关联不上为 null，前端只显示日期
            "kickoff": info.get("kickoff") if info else None,
            # 同理，编不出来就是 None，前端显示「–」如实告知，不猜队名
            "fixture_id": fid,
            # 为什么编不出来 —— 与竞彩页同一个字段名、同一个来源
            "fixture_match": info.get("fixture_match") if info else None,
            "had": _had(m.get("h"), m.get("d"), m.get("a")),
            # 庄家盘（Pinnacle 优先）。没预测过 / 赔率没拉到就是 None，
            # 前端显示「–」——「这场预测还没跑」是要让人看见的状态，不是错误。
            "book": rec.get("book"),
            # 接口预测的结果。字段名与 /api/predictions 的 xgb / llm 块一致，
            # 前端一套渲染函数同时画「首屏读到的」和「刚跑完写回的」。
            # None = 这场没跑过预测，页面显示「未跑」。
            "pred": rec.get("pred"),
            "llm": rec.get("llm"),
            "result": res or None,
            "result_label": _RESULT_LABEL.get(res),
            "score": (m.get("czScore") or "").strip() or None,
            "half_score": (m.get("czHalfScore") or "").strip() or None,
        })

    return {
        "lottery_name": s.get("lotteryGameName") or "胜负游戏",
        "issue": s.get("lotteryDrawNum"),
        "sale_begin": s.get("lotterySaleBegintime"),
        "sale_end": s.get("lotterySaleEndtime"),
        "draw_time": s.get("lotteryDrawTime"),
        "estimate_draw_time": s.get("estimateDrawTime"),
        "total_count": len(matches),
        # 让前端能如实说明「14 场里有几场拿得到开球时间」
        "kickoff_matched": sum(1 for m in matches if m["kickoff"]),
        "drawn_count": sum(1 for m in matches if m["result"]),
        "matches": matches,
    }


def _archive_best_effort(payload: dict, force: bool = False) -> dict | None:
    """把当前这期落库。**旁路**：任何异常都只记日志后返回 None，绝不影响页面。

    存档为什么要绑在"看一眼"上：上游只给当前期，停售/开奖/翻期之后这 14 场的
    对阵、赔率、彩果全没了 —— 用户不会为了复盘去跑 CLI，所以打开页面就是唯一的
    存档时机。这也是不装定时器的代价，见 service 模块头的说明。
    """
    issue = str((payload or {}).get("issue") or "").strip()
    if not issue:
        return None
    if (not force and _archived["issue"] == issue
            and time.time() - _archived["at"] < _ARCHIVE_TTL):
        return None
    try:
        r = sfc_archive_service.archive_payload(payload)
        _archived.update(issue=issue, at=time.time())
        return r
    except Exception as e:
        logger.warning(f"胜负彩存档失败(不影响页面显示): {type(e).__name__}: {e}")
        return None


@router.get("/sfc/matches")
async def sfc_matches(refresh: bool = False):
    """胜负彩（14 场胜负游戏）实时场次 + 胜平负赔率（去水）。与体彩官网保持一致。"""
    now = time.time()
    if not refresh and _cache["payload"] is not None and now - _cache["at"] < _CACHE_TTL:
        payload, flags = _cache["payload"], {"cached": True}
    else:
        try:
            payload = await asyncio.to_thread(_build_payload)
        except Exception as e:
            logger.error(f"胜负彩接口拉取失败: {type(e).__name__}: {e}")
            # 拉不到时退回上一次结果，避免页面直接空白
            if _cache["payload"] is None:
                raise HTTPException(status_code=502, detail=f"胜负彩接口拉取失败：{e}")
            payload = _cache["payload"]
            flags = {"cached": True, "stale": True, "error": f"{type(e).__name__}: {e}"}
        else:
            _cache.update(at=now, payload=payload)
            flags = {"cached": False}

    # 三条路径都存档：退回旧数据时存的是旧数据，但那仍然是"我们手上最好的那一份"，
    # 而且 archive_payload 是幂等 upsert，存一次旧的不会覆盖掉已有的新值。
    await asyncio.to_thread(_archive_best_effort, payload)
    return {**payload, **flags}


@router.post("/sfc/archive")
async def sfc_archive_now():
    """手动存档本期（SFC 页的「存档本期」按钮）。强制真拉一次上游，绕过 120 秒防抖。"""
    try:
        payload = await asyncio.to_thread(_build_payload)
    except Exception as e:
        logger.error(f"胜负彩存档拉取失败: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"胜负彩接口拉取失败：{e}")

    _cache.update(at=time.time(), payload=payload)
    r = await asyncio.to_thread(_archive_best_effort, payload, True)
    if r is None:
        raise HTTPException(status_code=502, detail="存档失败，详见后端日志")
    return {**r, "lottery_name": payload.get("lottery_name"),
            "sale_end": payload.get("sale_end")}


@router.get("/sfc/issues")
async def sfc_issues(page: int = 1, page_size: int = 20, date: str | None = None):
    """归档过的每期胜负彩 + 每期复盘（每个头错几场 / 错在哪）。给「历史」页的分类下拉用。

    顺带存档当前期：能看到历史页的这一刻，当前期的数据还在手上 —— 存档的成本是
    一次 14 行 upsert，比"下期开始这期就没了"便宜太多。拉不到只警告，不影响读历史。
    """
    now = time.time()
    if _cache["payload"] is None or now - _cache["at"] >= _CACHE_TTL:
        try:
            payload = await asyncio.to_thread(_build_payload)
            _cache.update(at=now, payload=payload)
        except Exception as e:
            logger.warning(f"看胜负彩历史时顺手存档失败(不影响读历史): {type(e).__name__}: {e}")
        else:
            await asyncio.to_thread(_archive_best_effort, payload)
    else:
        await asyncio.to_thread(_archive_best_effort, _cache["payload"])

    return await asyncio.to_thread(sfc_archive_service.list_issues, page, page_size, date)


# ── 下注前校验器（2026-09-29 立）────────────────────────────────────────
# 结论的【存储】在表 sfc_check_rules，逻辑在 app/services/sfc_check_service.py。
# 这里只负责把「要求 + 本期实测 + 逐条判定」端出来。
#
# 为什么加一个 format=text：接口是给人（和我）读的，JSON 里 requirement 是长文，
# 逐条读起来要来回翻。text 模式把同一份内容排成能直接粘进笔记的体检报告 ——
# 两个模式返回的是同一次计算的结果，不存在两套数。

def _wrap(s, width=76, indent=8):
    """按显示宽度折行。中文按 2 列算 —— 直接 textwrap 会按【字符数】折，
    中文行会超出终端一半。返回值一律 ASCII 缩进 + 原文，不引制表符。"""
    out, line, w = [], "", 0
    for ch in str(s or ""):
        cw = 2 if ord(ch) > 0x2E80 else 1
        if ch == "\n" or w + cw > width:
            out.append(line)
            line, w = "", 0
            if ch == "\n":
                continue
        line += ch
        w += cw
    if line:
        out.append(line)
    pad = " " * indent
    return "\n".join(pad + x for x in out)


def _render_check(d: dict) -> str:
    CALIB = sfc_check_service.CALIB_K
    L = []
    L.append("=" * 68)
    L.append(f" 胜负彩下注前校验   期 {d.get('issue')}   停售 {d.get('sale_end')}")
    L.append("=" * 68)
    m = d.get("metrics") or {}
    # 🔴 必须印出【用的是哪套盘】: 体彩齐全时用体彩, 不齐则整体回退庄家 Pinnacle。
    #    两套盘的去水概率不等(devig 是「方法,公司」配对问题), 闷掉来源 = 让读者
    #    以为所有报告都是同一个口径。
    L.append(f" 赔率覆盖 {m.get('odds_coverage')}/14 [{_ODDS_SRC.get(m.get('odds_src'), '?')}]"
             f"   胆 {m.get('bold_count')} 场   "
             f"已开奖 {m.get('drawn_count')}/14   抽水 {(m.get('vig') or 0) * 100:.2f}%")
    if m.get("odds_src") == "pinnacle":
        L.append(" ⚠️ 体彩本期不齐 -> 已【整体】改用庄家 Pinnacle 去水。"
                 "不得与体彩口径的期次直接比数。")

    # [3] 的实测数字随 [3] 那节一起出 —— 这是唯一算钱的地方，别让它飘到报告末尾
    roi_lines = []
    roi = m.get("roi")
    if roi:
        roi_lines = ["", "  [3] 实测（每期枚举 3^14 个组合，在 n 和 a 上取最优）",
                     f"       T = {_T_LABEL.get(roi.get('T_source'), '?')}"
                     f" = {roi['T']:,.0f} 注（单注 2 元）",
                     f"       {'a':>6}{'模型ROI':>11}{'修正ROI':>11}   修正 = 模型票数 x{CALIB:g}"]
        for row in roi["by_a"]:
            mark = "   <- 取这行" if row["a"] == roi["a"] else ""
            roi_lines.append(f"       {row['a']:>6.2f}{row['model_roi'] * 100:>10.1f}%"
                             f"{row['calib_roi'] * 100:>10.1f}%{mark}")
        roi_lines.append(f"       最优组合 P = {roi['p']:.2e}，模型说彩民押 {roi['m_model']:.1f} 注，"
                         f"x{CALIB:g} 修正后 {roi['m_calib']:.1f} 注")

    cur = None
    sec_name = {"0": "[0] 硬门槛（与 a 无关，主承重墙）",
                "1": "[1] 结构体检", "2": "[2] 选号（a 完全【不参与】）",
                "3": "[3] 押冷门组合（唯一算钱的一节）", "4": "[4] 反伪影四问",
                "X": "[X] 这个校验器判不了什么"}
    for r in d.get("rules") or []:
        if r["section"] != cur:
            if cur == "3":
                L.extend(roi_lines)
            cur = r["section"]
            L.append("")
            L.append(sec_name.get(cur, f"[{cur}]"))
        head = f"  {r['status']:<4} {r['code']:<6} {r['title']}"
        # 只给【改过】的规则挂变更行（rev>1）。18 条全挂会把报告淹掉。
        if int(r.get("revision") or 1) > 1:
            head += (f"\n       [变更 rev{r['revision']}  {r['changed_at']}  "
                     f"经 {r['approved_by'] or '-'} 同意]"
                     f"\n       原因：{r.get('change_reason') or '-'}")
        if r["metric"]:
            head += f"    [{r['metric']}={r['metric_value']} {r['op']} {r['threshold']}"
            # 方向要写出来：极性 -1 的行，条件【命中】才是坏消息，不标就会读反
            head += "  命中即否决]" if r.get("polarity") == -1 else "]"
        L.append(head)
        if r.get("reason"):
            L.append(f"        ({r['reason']})")
        if r["status"] in ("FAIL", "WARN", "SKIP") or r["section"] in ("3", "4", "X"):
            L.append(_wrap(r["requirement"]))
            if r.get("evidence"):
                L.append(_wrap("证据：" + r["evidence"], indent=8))

    s = d.get("summary") or {}
    L.append("")
    L.append("=" * 68)
    L.append(f" {d.get('verdict')}")
    L.append("=" * 68)
    for k in ("FAIL", "WARN", "SKIP"):
        for x in s.get(k) or []:
            L.append(f"  {k:<4} {x}")
    L.append("")
    L.append("  判不了：这期冷不冷（期级自相关 R^2=0.16%）/ 该选哪 14 场 / 主任为什么选这些比赛。")
    L.append("  下注前：结论记进 tools/bet_journal.py；主推唯一，赛前不中途改标。")
    # 🔴 只有这一行，【不带任何号】：一个逐场吐 3/1/0 的块就是选号建议，
    #    而 X-2 写的是「本接口只做体检和算钱，不产出选号建议」。出票走独立端点。
    L.append(f"  复式单测算（只算钱，不给建议）：GET /api/sfc/ticket"
             f"{('?issue=' + str(d['issue'])) if d.get('issue') else ''}")
    return "\n".join(L)


def _render_ticket(d: dict) -> str:
    """复式单测算的可读报告。

    🔴 表头一律 ASCII：中英混排按【字符数】补齐会错位（中文占 2 列但只算 1 个字符），
       中文说明放表头下面的图例行，不放进列头。
    """
    L = []
    L.append("=" * 80)
    L.append(f" 胜负彩复式单测算   期 {d.get('issue')}   停售 {d.get('sale_end')}")
    L.append("=" * 80)
    L.append(_wrap(d.get("note") or ""))
    L.append(f" T = {_T_LABEL.get(d.get('T_source'), '?')}"
             f" = {d.get('T') or 0:,.0f} 注（单注 2 元）")
    L.append(f" 去水用盘 = {_ODDS_SRC.get(d.get('odds_src'), '?')}"
             f"（{d.get('odds_coverage')}/14 场）")
    if d.get("odds_src") == "pinnacle":
        L.append(_wrap("⚠️ 体彩本期不齐（停售即撤盘，历史期常态），已【整体】改用庄家 "
                       "Pinnacle 去水。两套盘的去水概率不等，本页数字不得与体彩口径的期次直接比。"))
    for v in (d.get("basis") or {}).values():
        L.append(_wrap("口径：" + v))
    if d.get("odds_missing"):
        L.append(f" 缺赔率的场次：{d['odds_missing']}")
    L.append("")
    if not d.get("tickets"):
        L.append(" 没有可算的预算档。")
        return "\n".join(L)
    L.append(f"  {'budget':>8}{'notes':>7}{'cost':>9}{'E[hit]':>7}{'P(all)':>10}"
             f"{'actual':>7}{'ROI model':>15}{'ROI x3':>15}")
    for t in d.get("tickets") or []:
        if t.get("error"):
            L.append(f"  {t['budget']:>8,}   {t['error']}")
            continue
        act = f"{t['actual_hits']}/14" if "actual_hits" in t else "-"
        L.append(f"  {t['budget']:>8,}{t['notes']:>7,}{t['cost']:>9,}"
                 f"{t['expect_hits']:>7.2f}{t['p_all']:>10.2e}{act:>7}"
                 f"{t['roi_model_lo'] * 100:>8.1f}~{t['roi_model_hi'] * 100:<6.1f}"
                 f"{t['roi_calib_lo'] * 100:>8.1f}~{t['roi_calib_hi'] * 100:<6.1f}")
    L.append(_wrap("列义：budget 预算(元) / notes 注数 / cost 成本(元) / E[hit] 期望命中场数"
                   " / P(all) 全中概率 / actual 真实彩果命中 / ROI 区间取 a=1.2~1.5",
                   indent=6))
    L.append("")
    L.append("  票面（1..14 场；一场写 1 个号=单挑，2 个号=双选，3 个号=全包）：")
    for t in d.get("tickets") or []:
        if t.get("error") or not t.get("face"):
            continue
        L.append(f"    {t['budget']:>8,} 元 = {t['notes']:>6,} 注   {t['face']}")
    L.append("")
    L.append(_wrap("🔴 H0-2（表里已批准）：任何预算档 ROI 全为负，这是 36% 抽水本身。"
                   "加预算确实能买到更高的期望命中，ROI 也会略向 0 靠，"
                   "但每一档都远在 0 以下 —— 指望靠加钱翻正是没有出路的。"))
    L.append(_wrap("（别和 F4-3 的「买更多只会更差」混起来：那条说的是买一堆冷门组合，"
                   "买越多选到的越差；这里买的是覆盖面，两回事。）"))
    L.append(_wrap("这一块只算钱，不回答「该买哪个」；「买多少」也是你自己定的。"))
    return "\n".join(L)


@router.get("/sfc/check/rules")
async def sfc_check_rules():
    """只读规则表：胜利彩下注前的【要求】原文，不跑任何计算。

    表 sfc_check_rules，一是「要求写清楚」，二是每条背后的测量（evidence）与要求存在
    一起 —— 改阈值必须连证据一起改。要看这期过不过，用 GET /api/sfc/check。
    """
    rows = await asyncio.to_thread(sfc_check_service.list_rules)
    # 🔴 把「代码改了但没生效」摆到面上：不一致 = 那处改动没有经用户同意写进表，
    #    表里仍是已批准的版本。见 sfc_check_service 模块头的变更纪律。
    drift = await asyncio.to_thread(sfc_check_service.diff_against_code)
    return {"count": len(rows), "rules": rows,
            "code_drift": drift,
            "change_rule": ("规则改动必须先经用户同意，走 "
                            "tools/sfc_rule.py change --reason --approved-by；"
                            "日期由代码取 now() 写进 changed_at。")}


@router.get("/sfc/check/changelog")
async def sfc_check_changelog(code: str | None = None, limit: int = 50):
    """规则变更流水：哪条、哪天、为什么、经谁同意。只增不改。"""
    rows = await asyncio.to_thread(sfc_check_service.changelog, code, limit)
    return {"count": len(rows), "changes": rows}


@router.get("/sfc/check")
async def sfc_check(issue: str | None = None, format: str = "json"):
    """对照规则表校验某一期（默认最新一期）：要求 + 本期实测指标 + 逐条判定。

    format=text 返回同一份内容的可读报告（给人粘贴用），数据完全一致。
    """
    d = await asyncio.to_thread(sfc_check_service.check, issue)
    if format == "text":
        from fastapi.responses import PlainTextResponse
        if d.get("error"):
            return PlainTextResponse(f"校验失败：{d['error']}", status_code=404)
        return PlainTextResponse(_render_check(d))
    if d.get("error"):
        raise HTTPException(status_code=404, detail=d["error"])
    return d


@router.get("/sfc/ticket")
async def sfc_ticket(issue: str | None = None, budget: float | None = None,
                     format: str = "text"):
    """复式单测算：给定预算，本期买到什么票面、成本多少、期望回报率多少。

    🔴 为什么是【独立端点】而不是塞进 /sfc/check：X-2 表里的原文是「本接口只做体检和
       算钱，不产出选号建议」，一个逐场吐 3/1/0 的块就是选号建议。分开则两条都成立。

    🔴 为什么这块不新增规则：H0-2 已经批准过同一个结论（「复式回测所有预算档 CI 全
       跨不到 0……买越多越趋近 -36.00%」）。这里只是把那一句变成每期现场算一遍。

    budget 单位是【元】（成本），不传则印 128 / 512 / 2048 三档阶梯。
    """
    d = await asyncio.to_thread(sfc_check_service.ticket, issue, budget)
    if format == "text":
        from fastapi.responses import PlainTextResponse
        if d.get("error"):
            return PlainTextResponse(f"复式单测算失败：{d['error']}", status_code=404)
        return PlainTextResponse(_render_ticket(d))
    if d.get("error"):
        raise HTTPException(status_code=404, detail=d["error"])
    return d
