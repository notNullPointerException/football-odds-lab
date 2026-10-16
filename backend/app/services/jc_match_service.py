# -*- coding: utf-8 -*-
"""体彩场次 -> ai-football fixture_id 的解析。

【为什么要有这一层】
体彩给的是中文简称（荷兰 / 德国），而 fixtures 表里大量队名是拉丁文
（Netherlands vs Germany）。旧匹配拿体彩中文去比 fixture 的 home_name/away_name
—— 中文比拉丁，重合度恒为 0，算法在那个方向上根本没有信号。
2026-09-24 实测：8 场库里明明有货的欧国联/国际赛，全部因此配不上，页面
fixture_id 显示 null，点不进预测页。而页面上有这场比赛，就必须能预测。

【怎么修】换名源，不换算法。
teams 表同时有 name（拉丁）和 name_zh（中文）两列。拿体彩中文去比 teams.name_zh，
就是中文比中文；只有 name_zh 为空时才退回 fixtures.home_name（它有时也是中文）。
定位用 (home_id, away_id) —— 同一对主客队在同一时刻只可能有一场，不会像
「五场欧国联全排在 02:45」那样撞车。旧算法会被时间窗内的多场候选互相拉平。

【兜底一】teams.name_zh 覆盖率约 44%，中文比不出的走**体彩英文缩写码**（`_decide_by_code`）。
体彩的 `homeTeamCode` 与 `teams.code` 是两套约定，用公共前缀比 + 窗内唯一胜出。
2026-10-07 实测：在售 12 场里队名路线只配上 7 场，加码兜底后 10 场，逐场核验全对。

【兜底二】仍判不出的走 overrides 人工表，不猜。人工指定不仅用于「算法判不出」，
也用于「算法判错了」，所以它排在最前面。

只读，不写任何表。
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import text

# 开球时间容差（秒）。与 tools/build_jc_map.py 保持一致。
TIME_TOL = 600
# 队名重合度下限
MIN_SCORE = 0.50
# 第一名需领先第二名的分差，否则算平手未拉开
MARGIN = 0.25

_OVERRIDES_FILE = Path(__file__).resolve().parents[2] / "tools" / "jc_overrides.json"

CJK = re.compile(r"[一-鿿]")


def _cjk_set(s: str) -> set:
    return set(CJK.findall(s or ""))


def _side_score(jc_name: str, db_name: str):
    """单侧中文重合度：交集 / 较长者长度。None = 这侧无从比较。

    两侧都是拉丁名时返回 None（不是 0 分）—— 那不是「不匹配」，是「没法比」，
    硬算 0 分会把正确候选压到阈值以下。返回 None 让调用方把该侧排除出均值。
    """
    sa, sb = _cjk_set(jc_name), _cjk_set(db_name)
    if not sb:
        return None
    if not sa:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _code_sim(a: str, b: str) -> float:
    """两个三字母码的【公共前缀比】。0.0 = 首字母就不同。

    体彩的 `homeTeamCode` 与 API-Football 的 `teams.code` 是**两套缩写约定**，
    只有一部分重合：

        相同: GNI=Gnistan  BOT..?  FLA=Flamengo  FLU=Fluminense  HJK  KUP
        不同: 体彩 SAT(桑托斯) vs 库 SAN   BOF(博塔弗戈) vs BOT   GRM(格雷米奥) vs GRE
              体彩 VTL(维多利亚) vs 库 VIT   CHC(沙佩科) vs CHA   PAE(帕尔梅拉斯) vs PAL

    所以**全等会漏掉一大半**，必须放宽到前缀。实测 2026-10-07 在售 12 场：
    全等只能定位 7 场，前缀比 >= 2/3 能定位 10 场，且逐场人工核对全部正确。
    """
    a, b = (a or "").strip().upper(), (b or "").strip().upper()
    if not a or not b:
        return 0.0
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n / max(len(a), len(b))


# 一对码要算「认得出这支队」的下限。2/3 是实测下限：GRM/GRE、VTL/VIT、BHA/BAH
# 都是 0.667 且逐场核验正确；再降到 1/3 就开始把无关球队认成一支。
MIN_CODE_SCORE = 0.66


def load_overrides() -> dict:
    """人工兜底表：{"<销售日> <体彩编号>": fixture_id}。读不到就当空表。"""
    try:
        raw = json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in raw.items() if v}
    except Exception as e:
        logger.debug(f"读取 jc_overrides.json 失败(不影响自动匹配): {e}")
        return {}


def _parse_kickoff(s: str):
    s = (s or "").replace("T", " ")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def _candidates_between(db, lo: datetime, hi: datetime):
    """[lo, hi] 区间内的候选 fixture，带两侧队名的中文。

    名源优先 teams.name_zh，为空才退回 fixtures 上的 home_name/away_name ——
    这一行就是整个修复的核心。
    """
    return db.execute(text("""
        SELECT f.id, f.league_name, f.date, f.home_id, f.away_id,
               f.home_name AS home_raw, f.away_name AS away_raw,
               COALESCE(th.name_zh, f.home_name) AS home_zh,
               COALESCE(ta.name_zh, f.away_name) AS away_zh,
               th.code AS home_code, ta.code AS away_code
        FROM fixtures f
        LEFT JOIN teams th ON th.id = f.home_id
        LEFT JOIN teams ta ON ta.id = f.away_id
        WHERE f.date >= :lo AND f.date <= :hi
        ORDER BY f.date
    """), {"lo": lo, "hi": hi}).mappings().all()


def _candidates(db, kickoff: datetime, tol: int = TIME_TOL):
    """竞彩用：开球时刻 ±tol 秒的候选。"""
    return _candidates_between(db, kickoff - timedelta(seconds=tol),
                               kickoff + timedelta(seconds=tol))


def _parse_day(s: str):
    """"2026-09-29" / "2026-09-29 02:45:00" 都归零到当天 00:00。解不出返回 None。"""
    s = (s or "").strip().replace("T", " ")
    kt = _parse_kickoff(s)
    if kt is not None:
        return kt.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _decide(cands: list, home_zh: str, away_zh: str,
            scope: str = "窗内", scope_long: str = "时间窗内") -> dict:
    """在有候选的前提下，按两侧中文名重合度挑一个。

    scope / scope_long 只影响【说明文字】：竞彩是「时间窗内」，胜负彩是「当日」——
    两者窗口宽度差 144 倍，理由里必须能看出是哪条路径判的，否则出事没法回溯。
    cands 必须非空（空候选由各自的调用方给更贴切的理由）。
    """
    scored = []
    for c in cands:
        parts = [p for p in (_side_score(home_zh, c["home_zh"]),
                             _side_score(away_zh, c["away_zh"])) if p is not None]
        s = sum(parts) / len(parts) if parts else None
        scored.append((s, c))
    # None 排最后（当 -1 处理）
    scored.sort(key=lambda x: -(x[0] if x[0] is not None else -1))

    best_s, best = scored[0]
    second_s = scored[1][0] if len(scored) > 1 else None

    if best_s is None:
        # 两侧的名字都无从比对（name_zh 为空且 fixture 上也是拉丁名）
        if len(cands) == 1:
            return {"fixture_id": best["id"],
                    "how": f"两侧拉丁名无从比对，{scope_long}唯一"}
        return {"fixture_id": None,
                "how": f"两侧拉丁名无从比对，且{scope_long}有 {len(cands)} 个候选，无法定"
                       f"（补 teams.name_zh 或写 overrides）"}

    if best_s < MIN_SCORE:
        return {"fixture_id": None,
                "how": f"最佳队名重合度仅 {best_s:.2f} < {MIN_SCORE}"
                       f"（{scope} {len(cands)} 个候选，最佳 {best['home_zh']} vs {best['away_zh']}）"
                       f" → 多半是 teams.name_zh 缺失，写 overrides"}

    if second_s is not None and (best_s - second_s) < MARGIN:
        return {"fixture_id": None,
                "how": f"平手未拉开：{best_s:.2f} vs {second_s:.2f}"
                       f"（{best['home_zh']} / {scored[1][1]['home_zh']}）"}

    tag = f"队名重合 {best_s:.2f}"
    if len(cands) == 1:
        tag += f"（{scope}唯一）"
    elif second_s is None:
        # 窗内还有候选，但它们两侧都没有中文名可比 —— 拿不到反证，只能靠这一侧的正证。
        # 明说，别让它看起来像「领先很多」。
        tag += f"（{scope} {len(cands)} 候选，其余无中文名可比，没验过）"
    else:
        # 打【次佳的分】而不是分差 —— 「领先 0.00」会被读成「并列」，实际是次佳那场
        # 一个汉字都没对上，两道差得远。
        tag += f"（{scope} {len(cands)} 候选，次佳 {second_s:.2f}）"
    return {"fixture_id": best["id"], "how": tag}


def _decide_by_code(cands: list, home_code: str, away_code: str,
                    scope_long: str = "时间窗内") -> dict | None:
    """队名判不出时的兜底：用体彩的英文缩写码定位。判不出返回 None。

    【为什么需要】体彩给的是中文简称，库里大量队名只有拉丁名（`teams.name_zh`
    覆盖约 44%，巴甲/芬超几乎全空）。中文比拉丁重合度恒为 0，算法在那个方向上
    根本没有信号 —— 2026-10-07 在售 12 场里 5 场因此配不上。码是独立于语言的一路。

    【为什么用前缀不用全等】见 `_code_sim`：两套缩写约定只有部分重合。

    【为什么安全】这些码在**全库**层面高度歧义（`COR` 命中 6 支队、`CHA` 6 支、
    `GRE` 6 支），但 ±10 分钟窗内通常只有 2~8 场，判别力足够。因此这里有两道闸：
    ① 最佳得分 >= MIN_CODE_SCORE  ② 必须**严格**领先次佳（并列一律不猜）。
    过不了就返回 None —— 宁可判不出（页面显示 null、写 overrides），也不能猜错
    （猜错会把别场的赔率/模型挂到这场上，比 null 危险得多）。
    """
    hc, ac = (home_code or "").strip().upper(), (away_code or "").strip().upper()
    if not hc and not ac:
        return None

    scored = []
    for c in cands:
        sh = _code_sim(hc, c["home_code"])
        sa = _code_sim(ac, c["away_code"])
        # (排序分, 最强单侧分, 候选)
        scored.append((round(sh + sa, 4), round(max(sh, sa), 4), c))
    scored.sort(key=lambda x: -x[0])
    best_s, best_side, best = scored[0]
    # 🔴 闸门判的是【最强单侧】，不是总和。MIN_CODE_SCORE 那 2/3 是拿单侧实测定的
    #    （GRM/GRE 就是一侧 0.667）；若拿总和去比，两侧各只共享首字母
    #    （各 1/3、和 0.667）也会过闸 —— 那等于「首字母同就算认得出」，太薄。
    #    这是合成用例实测抓出来的（AZZ/DZZ vs ABC/DEF 必须拒），别改回总分。
    if best_side < MIN_CODE_SCORE:
        return None

    second_s = scored[1][0] if len(scored) > 1 else None
    if second_s is not None and best_s <= second_s:
        return None

    tail = f"，次佳 {second_s:.2f}" if second_s is not None else ""
    return {"fixture_id": best["id"],
            "how": f"队名判不出，体彩码兜底 {hc}/{ac} ≈ 库里 "
                   f"{best['home_code']}/{best['away_code']}"
                   f"（相似 {best_s:.2f}，{scope_long} {len(cands)} 候选{tail}）"}


def resolve(db, home_zh: str, away_zh: str, kickoff_str: str,
            day: str = None, num: str = None, overrides: dict = None,
            home_code: str = None, away_code: str = None) -> dict:
    """把一场体彩比赛解析成 fixture_id。

    返回 {"fixture_id": int|None, "how": str}。how 是给人看的解析依据，
    配不上时也写清楚为什么 —— 页面/日志都靠它，别静默返回 None。

    三层，前一层判不出才走下一层，**下层绝不覆盖上层的成功结果**：
        0) 人工 overrides
        1) 队名中文重合度（_decide）
        2) 体彩英文缩写码（_decide_by_code）—— 中文比拉丁时队名那条恒无信号
    """
    kt = _parse_kickoff(kickoff_str)
    if kt is None:
        return {"fixture_id": None, "how": "体彩没给开球时间，无从匹配"}

    # 0) 人工兜底优先。不仅用于「算法判不出」，也用于「算法判错了」，
    #    所以必须能覆盖后面所有分支。
    ov = load_overrides() if overrides is None else overrides
    fid = ov.get(f"{day} {num}")
    if fid:
        return {"fixture_id": fid, "how": "人工指定"}

    cands = _candidates(db, kt)
    if not cands:
        return {"fixture_id": None,
                "how": f"库里没有（{kt:%Y-%m-%d %H:%M} 前后 {TIME_TOL}s 内 0 场）→ 是同步问题，重建映射没用"}

    r = _decide(cands, home_zh, away_zh)
    if r["fixture_id"]:
        return r

    rc = _decide_by_code(cands, home_code, away_code)
    if rc:
        # 把队名那次的判词接在理由后面 —— 两条信号打架时，回溯时必须看得见另一条。
        rc["how"] += f"；队名路线判词: {r['how']}"
        return rc
    return r


def resolve_by_day(db, home_zh: str, away_zh: str, day: str,
                   day_key: str = None, num: str = None, overrides: dict = None) -> dict:
    """胜负彩专用：它【只给日期、不给开球时间】，走不了 resolve 的 ±10 分钟时间窗。

    实测（2026-09-28，期号 26134）：胜负彩的 startTime 与库里 fixtures.date 是
    同一个自然日 —— 9-29 那批 7 场全落在 9-29，9-30 那批 7 场全落在 9-30。
    所以窗口取整天。

    ⚠️ 当日候选有 170 场上下（是竞彩时间窗的几百倍），**完全靠 MIN_SCORE + MARGIN
    两道闸挡**。因此这条路径只在竞彩关联失败时才用（见 sfc.py），**永远不覆盖竞彩路径**，
    且 how 里写「当日」而不是「窗内」—— 两者窗口宽度差 144 倍，回溯时必须能分辨。

    overrides 的键：胜负彩没有「销售日」，用**比赛日**顶上，即
    `"2026-09-29 1"`（`<startTime 的日期> <matchNum>`）。
    🔴 别写成竞彩那种 `"2026-09-29 周日001"` —— 那是另一条路径的键，这里查不到。
    """
    d = _parse_day(day)
    if d is None:
        return {"fixture_id": None, "how": "胜负彩没给日期，无从匹配"}

    ov = load_overrides() if overrides is None else overrides
    # 🔴 day_key 不给就【退回解析出来的比赛日】。原来这里直接 f"{day_key} {num}"，
    #    而 sfc.py 不传 day_key → 键恒为 "None 1"，**人工指定这条路永远走不到**
    #    （overrides 里的键形如 "2026-09-20 周日024"，一个都对不上）。
    #    症状是「写了 overrides 却没生效」却没有任何报错，最难查的那种。
    key_day = day_key if day_key is not None else d.strftime("%Y-%m-%d")
    fid = ov.get(f"{key_day} {num}")
    if fid:
        return {"fixture_id": fid, "how": "人工指定"}

    cands = _candidates_between(db, d, d + timedelta(days=1) - timedelta(seconds=1))
    if not cands:
        return {"fixture_id": None,
                "how": f"库里没有（{d:%Y-%m-%d} 全天 0 场）→ 是同步问题，重建映射没用"}

    return _decide(cands, home_zh, away_zh, scope="当日", scope_long="当日")


def resolve_batch(db, matches: list, day: str) -> None:
    """就地给一批体彩 match dict 填上 fixture_id / fixture_match 说明。

    overrides 只读一次盘 —— 一场一读会把同一个文件读 50 遍，纯浪费。
    """
    ov = load_overrides()
    for m in matches:
        r = resolve(db, m.get("home", ""), m.get("away", ""), m.get("kickoff", ""),
                    day=day, num=m.get("match_num"), overrides=ov,
                    home_code=m.get("home_code"), away_code=m.get("away_code"))
        m["fixture_id"] = r["fixture_id"]
        m["fixture_match"] = r["how"]
