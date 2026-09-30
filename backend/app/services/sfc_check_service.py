# -*- coding: utf-8 -*-
"""胜负彩【下注前校验器】—— 把 2026-09-29 的结案结论存成表，由接口读出来逐条对照。

用户口径（2026-09-29）：「以后每次出了 14 场之后就对照今天的结论来校验，就相当于你这个
记录是检验器，下注之前必须校验」，随后加了存储要求：「把这个结论存到一个新的表中，
要求写的非常清楚明了，然后通过接口查询表中的要求」。

为什么要从记忆文件搬一张表出来，而不是继续只写在记忆里：
  ① 每期对照的是【同一份】要求 —— 结论留在对话里会随对话漂移，留在表里不会；
  ② 每条要求背后的测量（n / CI / 口径）与要求本身存在一起 —— 改数时必须连证据一起改；
  ③ 接口可以顺带把能算的指标算出来，判定不看人脸色（[3] 那一节的 ROI 是算出来的，
     不是我读完之后随口说的）。

表 sfc_check_rules：一行一条要求。
    metric / op / threshold 三列是「这条怎么自动判」；metric 为空 = 不自动判定，只展示
    要求原文（比如 [4] 的四问、以及「这个校验器判不了什么」那三行）。

🔴🔴 变更纪律（用户 2026-09-29 立）：
    「如果以后胜负14的这个结论有变化，你需要经过我的同意才能写到表中。
      每次变更需要加上当前日期、为什么变更。」

    三条硬规矩：
      ① **必须先拿到用户同意**，才能动表里任何一条结论。没同意就只准读、不准写。
      ② 每次变更**自动盖当前日期**（changed_at，由代码取 now()，不靠手填，填不了假）。
      ③ 每次变更**必须写明原因**（change_reason），没有原因不许写 —— apply_change()
         在 reason 为空时直接抛错，不是靠自觉。

    为什么落成机制而不是靠自觉：原来的设计是「表是 _RULES 的镜像，每次启动 sync 一遍」——
    那个设计下，我改一行代码、重启一次，结论就变了，**既没有同意也没有日期和原因**。
    所以现在改成：
      · 表是【已批准的结论】本身，代码 _RULES **只在建表那次**（表为空时）作为初始内容。
      · 表里一旦有行，ensure_tables() 只建表/补列，**一个字符都不改**。
      · 任何改动只能走 apply_change()（带 reason + approved_by），并留痕到
        sfc_check_rule_log（逐字段 old -> new）。
      · 代码与表不一致时不算「生效」，用 diff_against_code() / `tools/sfc_rule.py diff`
        把差异摆出来 —— 让「改了代码没批准」这件事**看得见**，而不是悄悄生效。

🔴 变更纪律（照抄记忆 football-sfc-prebet-checklist）：本表每一个参照值都对应一次实打实
   的测量。要改任何一个数，必须同时给出新测量（n、CI、口径）。别在别处"顺手更新"。
   **新测量写进 change_reason 和 evidence 里，两处都要。**

建表不进 alembic（与 sfc_issues / bet_journal / std_line 同思路，见 main.py:42 的说明）。
"""
import operator
import threading

import numpy as np
from loguru import logger
from sqlalchemy import text

from app.db.session import SessionLocal

# ── 2026-09-29 结案时定下的参照值 ────────────────────────────────────────
# 改这里任何一个数之前，先去看 _RULES 里对应的 evidence 字段。
PRIZE_RETURN = 0.6400        # 返奖率写死 64.00% -> 抽水 36.00%，20 年不变
TIER1_SHARE = 0.448          # 一等奖占销量比例
BOLD_P = 0.70                # 「胆」的阈值
BOLD_MEDIAN = 1              # 历史上每期 >=0.70 的场次中位数
BASE_HIT = "7.53/14"         # 全押市场热门 158 期的平均命中
BASE_ZERO = 13               # 158 期里一场没中的期数
A_LO, A_HI = 1.2, 1.5        # a 的区间（Poisson MLE 1.49 / 直接回归 1.18）
CALIB_K = 3.0                # 🔴 实测修正：P~6e-7 处模型说 2.3 注、实际 6.9 注
CAP = 5_000_000.0            # 单注封顶
_ROI_DP = 6                  # ROI 的落 JSON 精度 —— 见 _ticket_one 里的理由，别退回 4
NBIN = 50_000                # P 的分箱数

IDX = {"3": 0, "1": 1, "0": 2}   # 胜负彩彩果记法：3=主胜 1=平 0=客胜


# ── 规则表 ──────────────────────────────────────────────────────────────
# (section, code, title, kind, polarity, metric, op, threshold,
#  requirement, evidence, source)
#   kind: hard_fail 不过就是 FAIL / warn 不过只 WARN / info 只展示 / manual 要人工答 /
#         cannot 明确「判不了」（防我事后过度声称）
#   polarity: 1  条件成立 = 通过
#            -1  条件成立 = 【否决】。目前只有 H0-1 一条用：它的条件「抽水还是 36%」
#                成立时，结论恰恰是「不要下注」。不把方向显式写出来，判定就会反 ——
#                H0-1 曾把 PASS 当成「可以下注」，输出 "FAIL: []" 看着像放行。
_RULES = [
    # ── [0] 硬门槛 ──────────────────────────────────────────────────
    ("0", "H0-1", "抽水 36.00% 写死 —— 主承重墙（命中即否决）", "hard_fail",
     -1, "vig", ">=", "0.36",
     "胜负彩返奖率写死 64.00%（一等奖 44.8% + 二等奖 19.2% 的销量，按 70:30 分），"
     "20 年不动 ⇒ 抽水 36.00%，是竞彩（11.4%）的 3.16 倍。"
     "这一条不依赖 a、不依赖选号、不依赖任何模型，它是规则本身。"
     "🔴 若本期的目标是「赚钱」，到此就否决了 —— 后面四节都只是在 36% 里面挪几个百分点。"
     "唯一会推翻它的证据是体彩改了返奖率：重跑 football-sfc-prize-mechanics 里那条体检"
     "（959/1528 期恰好 64.00%）重测，没测出变化就照旧。",
     "959/1528 期返奖率恰好 64.00%；232 期恰好 19.2%（一等奖 0 注只派二等）",
     "football-sfc-prize-mechanics"),

    ("0", "H0-2", "历史最优买法（任意策略、任意预算）ROI 只有 -50% ~ -31%", "info",
     1, None, None, None,
     "复式回测所有预算档 CI 全跨不到 0，最好的一档（32,768 元/期）也只有 -40.4%；"
     "买越多越趋近 -36.00%。这不是选号不好，是 36% 抽水本身。"
     "⇒ 本校验器【不会】给出任何一个正 ROI 的建议；[3] 节算出来的一律是负的。",
     "全预算档 CI 跨 0；158 期复式回测", "football-sfc-prize-mechanics"),

    ("0", "H0-3", "本期总票数 T 必须等于 销量/2（否则整份报告不可信）", "hard_fail",
     1, "t_consist", "<=", "0.02",
     "报告里所有算钱的量（[3] 的 m、[5] 的票面 ROI）都建立在 T = 本期总票数上，"
     "而 T 的定义是死的：单注 2 元 ⇒ T = 销量/2。这一条把那个定义钉成判据 —— "
     "已开奖期若 |T×2/销量 − 1| > 0.02，说明报告用的 T 不是销量/2，"
     "[3]/[5] 全部作废，先修代码再看结论。赛前（销量未出）无从比对，判 SKIP。"
     "🔴 2026-09-30 实测教训：老代码写的是 T = 销量/0.192/2，把 0.192（【二等奖占销量的"
     "比例】，只在手上拿到的是【奖金池】时才该除）当成换算系数，T 被静默放大约 5.2 倍，"
     "错了很久没人发现。这一条就是为那类错误设的绊线。",
     "2026-09-30 全量实测 n=1760 期：一等奖池/(0.448×销量) 中位偏差 0.0007%、"
     "二等奖池/(0.192×销量) 中位偏差 0.0044% ⇒ total_sale_amount 就是销量，T=销量/2。"
     "触发例：老代码在 26131 上给出 t_consist = 4.208（远超 0.02）",
     "football-sfc-prebet-checklist"),

    # ── [1] 结构体检 ────────────────────────────────────────────────
    ("1", "S1-1", "赔率必须 14/14 齐，否则组合层没法枚举", "warn",
     1, "odds_coverage", "==", "14",
     "胜负彩只有【正在售卖的竞彩场次】才有体彩盘，历史上常常只有 8/14 场开得出赔率。"
     "少一场就没法枚举 3^14 个组合 ⇒ [3] 节（唯一算钱的那一节）直接跑不了。"
     "补齐的路：① 打开胜负彩页点「存档本期」重拉（抓的是当时的实时价）"
     "② 停售后重存档【无效】—— 体彩停售即撤盘，jc_* 全 NULL，不可补"
     "③ 看缺的场次 infohub_match_id 分不出「永远没有」和「窗口没到」，别拿它当判据："
     "本表 46662 行里 =0 出现 0 次（根本没这个状态），实际只有 >0（存档时体彩确实开了"
     "竞彩线）与 NULL（存档时没匹配上）；而且 NULL 不等于「体彩没开」——期 26134 缺的 8 场"
     "全是 NULL，其中 7 场事后仍在竞彩售卖中。要判只能拿队名去竞彩接口 GET /api/jc/matches 对；"
     "对不上的那场就是永远补不来，别等。"
     "④ 2026-09-30 起校验器有【整体回退】：体彩 had_* 不齐时改判庄家盘 odds_*（Pinnacle），"
     "判据是「另一套覆盖得更全才切，平手保持体彩」，且绝不逐场混用两套盘。"
     "⑤ 因此 odds_coverage 现在是【选用那套盘的覆盖数】，不再恒等于体彩覆盖数；"
     "报告头会印出用的是哪套。一旦印出 [庄家Pinnacle(体彩不齐,整体回退)]，"
     "本期的数与体彩口径的期次【不可直接比较】（两盘去水水位不同：Pinnacle 3.7% vs 体彩 13.0%）。",
     "历史期常只有 8/14 场开得出竞彩盘。infohub_match_id 实测分布（2026-09-29，全表 46662 行）："
     "NULL 35702 / =0 0 / >0 10960 ⇒ 旧文案那条「=0 分支」在本表不存在；且 NULL 不区分"
     "「本来没开」与「没抓到」（26134 缺的 8 场全 NULL，其中 7 场当天在竞彩售卖中）。"
     "；2026-09-30 全量扫描 3333 期复核回退的实际影响：体彩 14/14 共 167 期（回退不触发）；"
     "体彩 <14 且库里有庄家盘的只有 1 期（26134，6/14 → 14/14）；无任何一期是「部分补齐」。"
     "⇒ 回退在当前档案里几乎不产生收益，它的价值是【前瞻性】的：救的是在售窗口"
     "（体彩本来就只开售一部分场次），而校验器恰恰只在该窗口被使用。"
     "另：predictions.fixture_id 实测唯一 869/869，但取数仍用标量子查询而非 LEFT JOIN，"
     "以免重复行悄悄改掉「行数 = 场次数」这个组合层地基。",
     "football-sfc-prebet-checklist"),

    ("1", "S1-2", "胆数 >=2，否则是「被迫押掷硬币」结构", "warn",
     1, "bold_count", ">=", "2",
     "「胆」= 单场最大去水概率 >=0.70 的场次。历史上每期胆数的【中位数是 1 场】"
     "⇒ 常态就是没有胆、被迫在 10 场上下掷硬币。"
     "这条不过只是说「这一期是最坏的结构」，不代表过了就能赚钱 —— 两者无关。",
     "158 期里 >=0.70 的场次中位 1 场", "football-sfc-prize-mechanics"),

    ("1", "S1-3", "本期必须还没开奖（否则这是复盘，不是下注前校验）", "warn",
     1, "drawn_count", "==", "0",
     "已有彩果的期次跑这个接口，输出的是复盘而不是校验 —— 而且 [3] 节会失去意义"
     "（中奖组合已知，等于开了卷子答题）。看到这条 WARN 就把结论当复盘读。",
     "—", "football-sfc-prebet-checklist"),

    # ── [2] 选号 ────────────────────────────────────────────────────
    ("2", "S2-1", "选号只取决于你自己的 P_i —— a 完全不参与", "info",
     1, None, None, None,
     "🔴 a 描述的是【彩民怎么分布】，选号只取决于 P_i。彩池制下"
     "「期望奖金 = 奖池 × P_i /（彩民份额 + 你的份额）」，"
     "a 只影响「中了分多少」，不影响「中不中」。"
     "「该选哪 14 个结果」这个问题，a 给不出任何输入。"
     "去水统一用【等比】——体彩是定额比例抽水（按比赛悬殊分 6 档，水位极差 0.04pp），"
     "等比是它的精确逆。",
     "体彩 6 档水位极差 0.04pp；18877 场 Brier 等比 0.58226 最优",
     "football-devig-method-choice"),

    ("2", "S2-2", "先打败「全押市场热门」这条线，否则不如不分析", "info",
     1, None, None, None,
     f"基准线：全押市场热门，158 期平均中 {BASE_HIT}，有 {BASE_ZERO} 期一场没中。"
     "任何选号分析必须先打败这条线。另外「每期没胆」（>=0.70 的场次中位 1 场）"
     "是结构性的，不是这一期运气不好。",
     f"158 期平均 {BASE_HIT}，{BASE_ZERO} 期 0 中", "football-sfc-prize-mechanics"),

    ("2", "S2-3", "禁用「上期冷/热推本期」与「参考往年同期」", "info",
     1, None, None, None,
     "期级自相关 r=+0.040、R²=0.16% ⇒ 「这期冷不冷」在数据上【不存在答案】，"
     "任何用上期温度推本期的说法都是噪声。"
     "时间结构的四层假设已全部否掉：同批球队重合 84% 但同一对阵仅 10.5%；"
     "月份主效应 SD 1.32pp；留一年交叉验证 5.29pp 比直接猜长期均值 5.00pp 还差 5.8%；"
     "根因是【年份噪声 2.08pp > 月份信号 1.32pp】。",
     "期级自相关 R²=0.16%；四层检验详见来源", "football-time-structure-dead-ends"),

    # ── [3] 冷门组合路径（唯一算钱的一节）────────────────────────────
    # op 写成「> 0」而不是「<= 0」+ 极性 -1：这条是【要求】，读作「修正后 ROI 必须为正」，
    # 与标题同向。极性 -1 留给 H0-1 那种「条件描述的是世界状态」的行。
    ("3", "S3-1", "押冷门组合：修正后 ROI 必须为正（否则这条赚钱路径不存在）",
     "hard_fail", 1, "best_roi_calib", ">", "0",
     "这是理论上唯一的赚钱路径，也是本校验器唯一会去算钱的地方。"
     "判据（n=1 时精确）：彩民在该组合上的票数 m < 0.448 × P × T，"
     "其中 P = 该组合真实概率（14 场去水概率连乘），T = 本期总票数 = 销量/2。"
     "模型给 m = P^a / S(a) × T，S(a) = Prod_k Sum_o q^a（闭式，不用真的枚举 3^14）。"
     "🔴 但「能赚」那一支已被【直接数据】证伪：模型在策略最优点（P≈6.24e-7）说彩民押 "
     "2.3 注，实测是 6.9 注。所以接口同时给「模型值」和「×3 修正值」，"
     "判定一律用【修正值】。"
     "⇒ 只有 a >= 1.6 才翻正，而没有任何估计量给出 1.6"
     "（直接回归 1.18 / Poisson MLE 1.49 / 旧记 1.369）。"
     "⚠️ 中奖超 1 万元要交 20% 偶然所得税，下面所有 ROI 都还没扣它，真实数更低。",
     "直接观测：P 在 4e-7~8e-7 那档实际 n1 均值 6.9 vs 模型 4.63（比值 1.49）；"
     "相邻档 2.08 / 1.30，全表不是平的 ⇒ 幂律在形状上有偏",
     "football-sfc-crowd-concentration"),

    ("3", "S3-2", "a 报区间 1.2~1.5，不报 1.369 这个点", "info",
     1, None, None, None,
     "结构稳、水平不稳。稳的：a>1 在每个估计量/每一年/每个子样本上都成立，"
     "bootstrap P(a>1)=100%；两式自洽（Poisson MLE a1=1.490 / a2=1.415，差 +0.075，"
     "CI95 [-0.047,+0.225] 含 0），逐年差都在 ±0.16 内 ⇒ 幂律这个函数形式没被否定。"
     "不稳的：Poisson MLE 1.490 / 对数残差 LS 1.215 / 单组合直接回归 1.181±0.044。"
     "🔴 ROI 对 a 是【刀锋】：a=1.12→-57.2%，1.37→-42.5%，1.49→-30.9%，1.93→+30.1%。",
     "bootstrap 2000 次；逐年 2020-2026", "football-sfc-crowd-concentration"),

    # ── [4] 反伪影四问 ──────────────────────────────────────────────
    ("4", "F4-1", "独中时 payout 被什么封顶？（奖池总量 / 500万单注 / 整数票）", "manual",
     1, None, None, None,
     "三个上界都会剪，而且剪掉的恰好是「赚钱」的那部分。"
     "500 万封顶在两个尺度上不一样：单注最优时【不】咬（314万<500万），"
     "买 EV>1 整片区时咬得很狠（-76.3% → -89.3%）—— 因为「EV>1 的冷门组合」="
     "没人陪中=你独吞=恰好撞封顶。",
     "158 期复式回测，500万封顶前后差 9~13pp。"
     "2026-09-30 全量实测 n=1760 期：0.448x销量 只是【名义】一等奖池，"
     "61.2% 的期落在 ±5% 内，其余三类偏离：① 230 期池=0（0 注中奖，全额滚存）"
     "② 302 期高于名义池 5% 以上（上期滚存进来）"
     "③ 151 期低于名义池 5% 以上（500万封顶剪掉），其中 174 期单注恰好 =5,000,000。"
     "抽查 170 条滚存链：150 条满足「本期池−名义池 ≈ 前连续 0 中奖期的名义池之和」"
     "（16112 溢出 4,461,535 vs 上期名义 4,461,537），"
     "其余多为封顶叠加（16106 实际恰好 10,000,000 = 2 注 x 500万）。"
     "触发例 16101：独中 1 注，名义池 6,489,926，实际封在 5,000,000。"
     "=> 名义池在 38.8% 的期上不是真实分布；封顶往下剪、滚存往上抬，"
     "两个方向都真实存在 —— 独中那一支不能当上限。",
     "football-sfc-crowd-concentration"),

    ("4", "F4-2", "公式里的 m 是不是当成了连续量？（分数票会让 payout/n 发散）", "manual",
     1, None, None, None,
     "真 payout 是【奖池 /（彩民票数 + 我的票数）】，你永远拿不到比整个奖池更多。"
     "把它当连续量会让 m_i→0 时发散成无穷大。我在这上面连栽三次，"
     "三个「能赚」的漂亮数字（+49.9% / +98098% / +140%）全是分数票发散伪影。",
     "三次伪影的复现记录", "football-sfc-crowd-concentration"),

    ("4", "F4-3", "自己的票有没有算进分母？（自稀释）", "manual",
     1, None, None, None,
     "预算从 1,000 注加到 4,782,969 注，ROI 从 -43.1% 掉到 -66.5% —— "
     "多出来的都是自己稀释自己。ROI 在 N=1,000 就到顶，买更多只会更差。",
     "预算档 1e3~4.78e6 的 ROI 表", "football-sfc-crowd-concentration"),

    ("4", "F4-4", "🔴 这个外推有【直接观测】支持吗？模型在这段区间做过校准检验吗？",
     "manual", 1, None, None, None,
     "前三条来自被【上界】剪掉的伪影，这一条来自被【直接数据】证伪的那一个"
     "（同日的 +56%）。「能赚」的漂亮数字出现时，第一反应是去找剪它的上界，"
     "而不是找理由相信它。这条不过，其余全废。",
     "S3-1 的 evidence 就是这次的直接观测", "football-sfc-crowd-concentration"),

    ("4", "F4-5", "中奖超 1 万元交 20% 偶然所得税 —— 所有 ROI 都还没扣", "info",
     1, None, None, None,
     "上面每一个 ROI 都是税前。超过 1 万元的奖金按 20% 计征 ⇒ 真实数还要再低一截。",
     "—", "football-sfc-prebet-checklist"),

    # ── 判不了什么（防我事后过度声称）─────────────────────────────────
    ("X", "X-1", "判不了「这期冷不冷」", "cannot",
     1, None, None, None,
     "期级自相关 R²=0.16%，这个问题在数据上【不存在答案】。"
     "接口不会给「本期偏冷/偏热」这种结论；给了就是编的。",
     "r=+0.040, R²=0.16%", "football-time-structure-dead-ends"),

    ("X", "X-2", "判不了「该选哪 14 场」", "cannot",
     1, None, None, None,
     "那是 [2] 节，取决于你自己的 P_i，与 a、与彩民分布无关。"
     "本接口只做体检和算钱，不产出选号建议。",
     "—", "football-sfc-prebet-checklist"),

    ("X", "X-3", "判不了「主任为什么选这些比赛」", "cannot",
     1, None, None, None,
     "选赛理由全是商业的（时段 / 知名度 / 凑盘子），与结果正交。"
     "反证在水位：知道得多该收更少的水，而体彩水位是 Pinnacle 的 3.5 倍。"
     "「既然选这些比赛肯定有原因」已被否。",
     "同场水位 Pinnacle 3.7% vs 体彩 13.0%（逐场 9.1~9.6pp 异常一致）",
     "football-jc-pinnacle-bias"),
]


_DDL = [
    """CREATE TABLE IF NOT EXISTS sfc_check_rules (
        id INT AUTO_INCREMENT PRIMARY KEY,
        seq INT NOT NULL DEFAULT 0 COMMENT '展示顺序',
        section VARCHAR(8) NOT NULL COMMENT '[0]硬门槛 [1]结构体检 [2]选号 [3]冷门组合 [4]反伪影四问 X判不了什么',
        code VARCHAR(16) NOT NULL COMMENT '稳定标识, 如 H0-1 / S3-1',
        title VARCHAR(128) NOT NULL COMMENT '一句话要求',
        kind VARCHAR(12) NOT NULL COMMENT 'hard_fail 不过=FAIL / warn / info / manual 要人工答 / cannot 判不了',
        polarity TINYINT NOT NULL DEFAULT 1 COMMENT '1=条件成立则通过; -1=条件成立即否决',
        metric VARCHAR(32) NULL COMMENT '绑定的指标名(见 evaluate 的 metrics); 为空=不自动判定',
        op VARCHAR(8) NULL COMMENT '>= <= == > < in:1,2,3',
        threshold VARCHAR(64) NULL COMMENT '阈值',
        requirement TEXT NOT NULL COMMENT '要求原文(要写清楚到不用回来问)',
        evidence TEXT NULL COMMENT '这条背后的测量: n / CI / 口径。改 threshold 必须连它一起改',
        source VARCHAR(64) NULL COMMENT '出处记忆名',
        is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '可在库里直接停用某条规则, 不用改代码',
        revision INT NOT NULL DEFAULT 1 COMMENT '改过几次',
        changed_at DATETIME NULL COMMENT '最后一次变更的日期, 由代码取 now() 写入',
        change_reason VARCHAR(512) NULL COMMENT '最后一次变更的原因(必填), 含新测量的 n/CI/口径',
        approved_by VARCHAR(64) NULL COMMENT '谁同意的, 如 用户 2026-09-29',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_code (code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='胜负彩下注前校验器: 一行一条要求'""",

    """CREATE TABLE IF NOT EXISTS sfc_check_rule_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(16) NOT NULL COMMENT '改了哪条规则',
        revision INT NOT NULL COMMENT '改完是第几版',
        field VARCHAR(24) NOT NULL COMMENT '改了哪个字段',
        old_value TEXT NULL,
        new_value TEXT NULL,
        change_reason VARCHAR(512) NOT NULL COMMENT '为什么改',
        approved_by VARCHAR(64) NOT NULL COMMENT '谁同意的',
        changed_at DATETIME NOT NULL COMMENT '变更日期',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_code (code),
        KEY idx_at (changed_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='胜负彩校验规则的逐字段变更流水(只增不改)'""",
]

# 🔴 给【已存在】的表补列。CREATE TABLE IF NOT EXISTS 对已存在的表是【空操作】——
# 上面 _DDL 里新加的列在老库上永远长不出来，必须显式 ALTER（与 sfc_archive_service 同）。
# MySQL 没有 `ADD COLUMN IF NOT EXISTS`（那是 MariaDB 的），只能先查 information_schema。
_NEW_COLUMNS = [
    ("sfc_check_rules", "revision", "INT NOT NULL DEFAULT 1 COMMENT '改过几次'"),
    ("sfc_check_rules", "changed_at", "DATETIME NULL COMMENT '最后一次变更的日期'"),
    ("sfc_check_rules", "change_reason", "VARCHAR(512) NULL COMMENT '最后一次变更的原因(必填)'"),
    ("sfc_check_rules", "approved_by", "VARCHAR(64) NULL COMMENT '谁同意的'"),
]

# 可以改的字段白名单。🔴 不是「防注入」那么浅 —— 它是「哪些算结论、哪些算杂项」的定义：
# 改了这里面的任何一个都算【结论变更】, 必须先拿到用户同意。
# `section` / `seq` 只决定这条规则【显示在哪一节、排第几】，不参与判定（判定看 kind），
# 所以放进白名单是安全的；新增规则时这两个是必填。
EDITABLE = ("section", "seq", "title", "kind", "polarity", "metric", "op", "threshold",
            "requirement", "evidence", "source", "is_active")
# 新增一条规则时【必须】显式给出的字段 —— 少了任何一条都会造出一条半成品门槛。
CREATE_REQUIRED = ("section", "title", "kind", "polarity")

# 表为空时灌进去的初始内容。非空表【绝对不动】—— 见模块头的变更纪律。
_INIT_REASON = "2026-09-29 建表初始：内容 = 当日结案结论，用户已确认（用户原话：下注之前必须校验）"


# ── 建表 / 灌规则 ────────────────────────────────────────────────────────
_ENSURED = False
_ENSURE_LOCK = threading.Lock()


def _ensure_columns(db):
    """幂等补列。跑多少次都只做「缺的那部分」。"""
    for table, col, spec in _NEW_COLUMNS:
        n = db.execute(text(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = DATABASE()
                 AND TABLE_NAME = :t AND COLUMN_NAME = :c"""),
            {"t": table, "c": col}).scalar()
        if not n:
            # 表名/列名/类型全是本模块的常量，不含外部输入
            db.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {spec}"))
            logger.info(f"sfc_check: 补列 {table}.{col}")


def ensure_tables():
    """建表 + 补列。**表非空时一个字符都不改**（见模块头的变更纪律）。

    幂等。改动只能走 apply_change()，那条路必须带 reason + approved_by 并留痕。
    """
    global _ENSURED
    db = SessionLocal()
    try:
        for ddl in _DDL:
            db.execute(text(ddl))
        _ensure_columns(db)

        # 🔴 只在【表为空】时灌初始内容 —— 也就是真正第一次建表那次。
        #    有行之后这里就再也不写任何东西了：结论只能经用户同意、走 apply_change 改。
        #    （旧设计是每次启动按 _RULES 覆盖一遍，那样改代码就能悄悄改结论。）
        n = db.execute(text("SELECT COUNT(*) FROM sfc_check_rules")).scalar()
        if not n:
            for i, (sec, code, title, kind, pol, metric, op, thr, req, ev, src) in enumerate(_RULES):
                db.execute(text("""
                    INSERT INTO sfc_check_rules
                        (seq, section, code, title, kind, polarity, metric, op, threshold,
                         requirement, evidence, source, is_active,
                         revision, changed_at, change_reason, approved_by)
                    VALUES (:seq, :sec, :code, :title, :kind, :pol, :metric, :op, :thr,
                            :req, :ev, :src, 1, 1, NOW(), :rsn, :by)"""),
                    {"seq": (i + 1) * 10, "sec": sec, "code": code, "title": title,
                     "kind": kind, "pol": pol, "metric": metric, "op": op, "thr": thr,
                     "req": req, "ev": ev, "src": src,
                     "rsn": _INIT_REASON, "by": "用户 2026-09-29"})
            logger.info(f"sfc_check: 首次建表，灌入 {len(_RULES)} 条初始规则")
        else:
            # 本表 2026-09-29 建表时还没有 revision/变更记录那几列 —— 给老行补上
            # 「建表初始」这条记录。幂等：只认 change_reason 还空着的行。
            # ⚠️ 这是补【历史】，不是改结论，所以不算一次变更。
            n = db.execute(text("""UPDATE sfc_check_rules SET revision=1, changed_at=NOW(),
                    change_reason=:r, approved_by=:b
                    WHERE change_reason IS NULL"""),
                {"r": _INIT_REASON, "b": "用户 2026-09-29"}).rowcount
            if n:
                logger.info(f"sfc_check: 给 {n} 条老规则补上「建表初始」变更记录")
        db.commit()
        _ENSURED = True
    finally:
        db.close()


# ── 变更（🔴 必须先拿到用户同意，见模块头）──────────────────────────────
def apply_change(code, changes: dict, reason: str, approved_by: str, changed_at=None,
                 allow_create=False):
    """改一条规则；allow_create=True 时也能**新增**一条。**唯一能改表的入口**，
    且 reason / approved_by 缺一不可。

    🔴 调用之前必须先拿到用户的同意。这个函数不会替你问，也拦不住你 -
       它能做的是保证「改了必须有日期、有原因、有留痕」，以及「没走这条路改不动表」。
    🔴 新增门槛会改变「什么算不合格」，比改一条更重，所以默认【拒绝新增】：
       必须调用方显式写 allow_create=True 才能建。getattr 那道坎是故意的，
       免得哪天顺手一句 apply_change(...) 就多出一条谁都没批过的硬门槛。
    changes: {字段: 新值}，字段必须在 EDITABLE 里；新增时 CREATE_REQUIRED 必须齐全。
    返回 {code, revision, changed_at, fields:[{field, old, new}], created_rule}
    """
    if not reason or not str(reason).strip():
        raise ValueError("变更必须写明原因（change_reason 不能为空）")
    if not approved_by or not str(approved_by).strip():
        raise ValueError("变更必须记录是谁同意的（approved_by 不能为空）")
    bad = [k for k in changes if k not in EDITABLE]
    if bad:
        raise ValueError(f"字段不在可改白名单里: {bad}; 允许 {list(EDITABLE)}")
    if not changes:
        raise ValueError("changes 为空，没有要改的东西")

    from datetime import datetime
    when = changed_at or datetime.now()
    _ensure_once()
    db = SessionLocal()
    try:
        # 🔴 FOR UPDATE：两个人同时改同一条时，后一个的 old_value 才不会是基于旧版本的
        cur = db.execute(text("SELECT * FROM sfc_check_rules WHERE code=:c FOR UPDATE"),
                         {"c": code}).mappings().first()
        created = cur is None
        rev = 1 if created else int(cur["revision"] or 1) + 1
        # 字段名来自白名单，不是外部输入，可以安全拼进 SQL
        sets = ", ".join(f"{k}=:{k}" for k in changes)
        params = dict(changes)
        params.update({"c": code, "rev": rev, "when": when,
                       "rsn": reason, "by": approved_by})
        if created:
            # 新增一条门槛会改变「什么算不合格」，属于结论变更；必须显式开闸。
            # 要加规则就说清楚再加 —— 也就是 allow_create=True + reason + approved_by 三样齐全。
            if not allow_create:
                raise ValueError(f"规则 {code} 不存在。新增规则会改变判定口径，"
                                 f"要新增必须显式 allow_create=True（CLI: add 子命令）。")
            missing = [k for k in CREATE_REQUIRED if k not in changes]
            if missing:
                raise ValueError(f"新增规则必须给全 {list(CREATE_REQUIRED)}，还缺 {missing}")
            fields = {"seq": 0, "metric": None, "op": None, "threshold": None,
                      "requirement": None, "evidence": None, "source": None,
                      "is_active": 1, **changes}
            db.execute(text("""INSERT INTO sfc_check_rules
                    (seq, section, code, title, kind, polarity, metric, op, threshold,
                     requirement, evidence, source, is_active,
                     revision, changed_at, change_reason, approved_by)
                    VALUES (:seq, :section, :c, :title, :kind, :polarity, :metric, :op,
                            :threshold, :requirement, :evidence, :source, :is_active,
                            :rev, :when, :rsn, :by)"""),
                {**fields, "c": code, "rev": rev, "when": when,
                 "rsn": reason, "by": approved_by})
        else:
            db.execute(text(f"""UPDATE sfc_check_rules SET {sets},
                    revision=:rev, changed_at=:when, change_reason=:rsn, approved_by=:by
                    WHERE code=:c"""), params)
        for k, v in changes.items():
            db.execute(text("""INSERT INTO sfc_check_rule_log
                (code, revision, field, old_value, new_value, change_reason,
                 approved_by, changed_at)
                VALUES (:c, :rev, :f, :old, :new, :rsn, :by, :when)"""),
                {"c": code, "rev": rev, "f": k,
                 "old": None if cur is None else (None if cur[k] is None else str(cur[k])),
                 "new": None if v is None else str(v),
                 "rsn": reason, "by": approved_by, "when": when})
        db.commit()
        return {"code": code, "revision": rev, "changed_at": str(when),
                "approved_by": approved_by, "reason": reason,
                "fields": [{"field": k,
                            "old": None if cur is None else cur[k], "new": v}
                           for k, v in changes.items()]}
    finally:
        db.close()


def changelog(code=None, limit=200):
    """变更流水（只增不改）。回答「这条规则什么时候、为什么、经谁同意改的」。"""
    _ensure_once()
    db = SessionLocal()
    try:
        sql = """SELECT code, revision, field, old_value, new_value, change_reason,
                        approved_by, changed_at
                 FROM sfc_check_rule_log {w} ORDER BY changed_at DESC, id DESC LIMIT :n"""
        w, p = ("WHERE code=:c" if code else ""), {"n": limit}
        if code:
            p["c"] = code
        return [dict(r) for r in db.execute(text(sql.format(w=w)), p).mappings()]
    finally:
        db.close()


def diff_against_code():
    """代码里的 _RULES 与表里的【已批准结论】有哪些不一致。

    🔴 存在的意义：让「改了代码但没经用户同意/没走 apply_change」这件事看得见。
       不一致 = 代码改的那部分【没有生效】，表里仍然是已批准的旧结论。
    """
    _ensure_once()
    db = SessionLocal()
    try:
        rows = {r["code"]: dict(r) for r in db.execute(
            text("SELECT * FROM sfc_check_rules")).mappings()}
    finally:
        db.close()
    out = []
    for (sec, code, title, kind, pol, metric, op, thr, req, ev, src) in _RULES:
        want = {"section": sec, "title": title, "kind": kind, "polarity": pol,
                "metric": metric, "op": op, "threshold": thr,
                "requirement": req, "evidence": ev, "source": src}
        have = rows.get(code)
        if have is None:
            out.append({"code": code, "status": "表里没有（未生效）",
                        "fields": list(want)})
            continue
        diff = [{"field": k, "code": v, "table": have.get(k)}
                for k, v in want.items() if str(have.get(k)) != str(v)]
        if diff:
            out.append({"code": code, "status": "代码与表不一致（表里是已批准的版本）",
                        "fields": diff})
    for code in rows:
        if code not in {r[1] for r in _RULES}:
            out.append({"code": code, "status": "表里有但代码没有", "fields": []})
    return out


def _ensure_once():
    """首次用到时才建表灌规则（与 sfc_archive_service / predict._fetch_jc_odds 同一个写法）。

    🔴 两道检查都要：等锁的线程进来时，先进去的那个可能已经建完了。
    """
    if _ENSURED:
        return
    with _ENSURE_LOCK:
        if _ENSURED:
            return
        ensure_tables()


# ── 指标计算 ────────────────────────────────────────────────────────────
def _q_prop(o):
    """等比例去水。体彩是定额比例抽水 ⇒ 等比是它的精确逆（见记忆）。"""
    q = [1.0 / x for x in o]
    s = sum(q)
    return [x / s for x in q]


def _load(issue):
    db = SessionLocal()
    try:
        if not issue:
            issue = db.execute(text("""
                SELECT i.issue FROM sfc_issues i
                WHERE EXISTS (SELECT 1 FROM sfc_issue_matches m WHERE m.issue = i.issue)
                ORDER BY i.issue DESC LIMIT 1""")).scalar()
        if not issue:
            return None, None, [], []
        info = db.execute(text("""SELECT issue, sale_end, total_sale_amount, draw_time
            FROM sfc_issues WHERE issue=:i"""), {"i": issue}).mappings().first()
        # 🔴 庄家盘那三列用【标量子查询】而不是 LEFT JOIN —— LEFT JOIN 一旦 predictions
        #    里出现重复 fixture_id，就会把行数悄悄翻倍，而【行数 = sfc 场次数】是后面
        #    所有组合层计算的地基（3^14 枚举、DP、投票）。标量子查询在结构上不可能改行数。
        #    （2026-09-30 实测 predictions.fixture_id 唯一 869/869，但仍不依赖这个假设。）
        rows = db.execute(text("""SELECT m.no, m.home, m.away, m.match_date, m.kickoff_at,
                m.had_h, m.had_d, m.had_a, m.cz_result, m.fixture_id, m.infohub_match_id,
                (SELECT p.odds_home FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_home,
                (SELECT p.odds_draw FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_draw,
                (SELECT p.odds_away FROM predictions p WHERE p.fixture_id=m.fixture_id) AS odds_away
            FROM sfc_issue_matches m WHERE m.issue=:i ORDER BY m.no"""),
            {"i": issue}).mappings().all()
        # T 的估计：近 5 期销量中位（本期销量要开奖后才有）。与 tools/sfc_prebet.py 同口径。
        recent = [float(r[0]) for r in db.execute(text("""SELECT total_sale_amount FROM sfc_issues
            WHERE total_sale_amount IS NOT NULL AND issue<:i
            ORDER BY issue DESC LIMIT 5"""), {"i": issue}).fetchall()]
        return issue, info, rows, recent
    finally:
        db.close()


def _collect(rows, cols):
    """按给定的赔率三列挑出「三列都 >1」的场次，返回 (have, miss)。"""
    have, miss = [], []
    for r in rows:
        o = [r[c] for c in cols]
        if all(v is not None and float(v) > 1.0 for v in o):
            have.append((r["no"], r["home"], r["away"], [float(v) for v in o]))
        else:
            miss.append({"no": r["no"], "home": r["home"], "away": r["away"],
                         "infohub_match_id": r["infohub_match_id"]})
    return have, miss


def _odds_metrics(rows):
    """赔率覆盖 / 胆数 / 最悬的 3 场。返回 (have, miss, bold, sharp, ps, src)。

    🔴 选盘口径：**优先体彩竞彩 `had_*`** —— 胜负彩是体彩的盘，它自己的价最贴。
       但体彩只在【在售窗口】开赔，停售即撤盘，于是历史期常年 6/14、永远算不了。
       所以加一层【整体回退】：体彩不齐就整套改用庄家 Pinnacle
       （`predictions.odds_*`），并把 `src` 一路送到报告面上标出来是用的哪套。

    🔴 只能【整体切】不能逐场混 —— 一半腿体彩一半腿庄家，组合概率就不是一个统一
       口径了（同 S1-1 的精神）。所以判据写成「另一套覆盖得**更多**才切」，
       平手保持体彩；绝不出现「这场体彩那场庄家」。

    ⚠️ 回退会改变 P 的来源，而两套盘的去水概率不等（[[football-devig-method-choice]]：
       devig 是 (方法, 公司) 配对问题）⇒ 所以 `src` 必须露在报告上，不能闷掉。
    """
    have, miss = _collect(rows, ("had_h", "had_d", "had_a"))
    src = "jc"
    if len(have) < len(rows):                       # 体彩不齐 -> 试庄家盘
        alt, alt_miss = _collect(rows, ("odds_home", "odds_draw", "odds_away"))
        if len(alt) > len(have):                    # 只有【更全】才换，平手保持体彩
            have, miss, src = alt, alt_miss, "pinnacle"
    ps = {h[0]: _q_prop(h[3]) for h in have}
    bold = [{"no": h[0], "home": h[1], "away": h[2], "p": max(ps[h[0]])}
            for h in have if max(ps[h[0]]) >= BOLD_P]
    order = sorted(ps.items(), key=lambda kv: -max(kv[1]))
    nm = {r["no"]: (r["home"], r["away"]) for r in rows}
    sharp = [{"no": no, "home": nm[no][0], "away": nm[no][1], "p": max(p)}
             for no, p in order[-3:]][::-1]
    return have, miss, bold, sharp, ps, src


def _estimate_T(info, recent):
    """本期总票数 T。单注 2 元 ⇒ T = 销量/2。

    🔴 别在销量上再除 0.192 —— 0.192 是【二等奖占销量的比例】，那个 /0.192/2 的换算
       只在手上拿到的是二等【奖金池】时才成立（池 / 0.192 = 销量，/2 = 票数）。
       sfc_issues.total_sale_amount 存的是上游 totalSaleAmount，本来就是销量
       （见 tools/sfc_history.py:_issue_row）。多除一次会把 T 放大约 5 倍。
       2026-09-30 全量实测 n=1760 期：一等池/(0.448*销量) 中位偏差 0.0007%、
       二等池/(0.192*销量) 中位偏差 0.0044% —— 销量就是销量。
       （/0.192 该用的地方是：手上拿到的是【奖金池】时才 池/0.192 = 销量。）
    开奖后本期销量已知，直接用本期真值；开奖前（本期销量未出）退到近 5 期中位。
    返回 (T, 来源标记)；两者都取不到时 (None, None)。
    """
    own = float(info["total_sale_amount"]) if info and info.get("total_sale_amount") else None
    if own:
        return own / 2.0, "own"
    if not recent:
        return None, None
    return sorted(recent)[len(recent) // 2] / 2.0, "median5"


def _best_roi(ps, have, info, recent):
    """[3] 节算钱：每期枚举一次 3^14 个组合概率，再在 n 和 a 上取最优。

    🔴 与 tools/crowd_roi.py / tools/sfc_prebet.py 是同一套算式（那两份是 CLI 孪生）。
       改了这里必须同步改那两份，反之亦然 —— 两个地方给出两个数是更坏的结果。
    返回 (model 最好 ROI, 修正后最好 ROI, 对应 P, 对应模型票数)。
    """
    if len(have) < 14:
        return None
    T, t_src = _estimate_T(info, recent)
    if T is None:
        return None
    Q = [ps[h[0]] for h in have]
    # 反复外积：3^k -> 3^(k+1)，枚举完是 4,782,969 个数（38MB，实测百毫秒级）
    P = np.array(Q[0])
    for k in range(1, 14):
        P = np.concatenate([P * Q[k][0], P * Q[k][1], P * Q[k][2]])
    P = P[P > 0]
    cnt, edges = np.histogram(np.log(P), bins=NBIN)
    ok = cnt > 0
    grid = np.exp((edges[:-1][ok] + edges[1:][ok]) / 2.0)     # 只留有组合落进去的箱
    out = []
    for a in (A_LO, 1.49, A_HI):
        S = float(np.prod([sum(x ** a for x in q) for q in Q]))   # S(a) 闭式
        best = None
        for n in (1, 10, 100):
            mT = (grid ** a / S) * T
            roi_mod = grid * np.minimum(TIER1_SHARE * T / (mT + n), CAP / 2.0) - 1.0
            roi_cal = grid * np.minimum(TIER1_SHARE * T / (mT * CALIB_K + n), CAP / 2.0) - 1.0
            k = int(np.argmax(roi_mod))
            row = (float(roi_mod[k]), float(roi_cal[k]), float(grid[k]), float(mT[k]), a)
            if best is None or row[0] > best[0]:
                best = row
        out.append(best)
    top = max(out, key=lambda x: x[0])
    return {"T": T, "T_source": t_src,
            "model_roi": round(top[0], _ROI_DP), "calib_roi": round(top[1], _ROI_DP),
            "p": top[2], "m_model": round(top[3], 2), "m_calib": round(top[3] * CALIB_K, 2),
            "a": top[4],
            "by_a": [{"a": r[4], "model_roi": round(r[0], _ROI_DP),
                      "calib_roi": round(r[1], _ROI_DP)}
                     for r in out]}


# ── 复式单测算 ──────────────────────────────────────────────────────────
# 🔴 这一块是 H0-2 的【现场实例化】，不是新结论：
#    H0-2 已批准：「复式回测所有预算档 CI 全跨不到 0……买越多越趋近 -36.00%」。
#    这里把「那一句」变成「本期这个预算具体是什么票面、什么 ROI」。
# 🔴 它故意【不放在 check() 里】：X-2 表里的原文是「本接口只做体检和算钱，不产出选号建议」，
#    一个逐场吐 3/1/0 的块就是选号建议。放独立端点则 X-2 逐字仍然成立。
SYM = ("3", "1", "0")                       # 场内序号 0/1/2 -> 彩果记法
TICKET_NOTES_MAX = 200_000                  # DP 与 ROI 都是 O(注数)，给个挡板
TICKET_BUDGET_LADDER = (128, 512, 2048)     # 不传 budget 时印这三档（元）
TICKET_A_GRID = (A_LO, 1.49, A_HI)          # ROI 报区间，不报单点


def _ticket_cands(q):
    """一场比赛的三个候选：概率最高的 1 / 2 / 3 个号。

    场内序号升序排列（0=客胜 1=平 2=主胜），只为票面好读，与概率大小无关。
    """
    order = np.argsort(-np.asarray(q, dtype=float))
    out = []
    for k in (1, 2, 3):
        sel = sorted(int(x) for x in order[:k])
        out.append((k, sel, float(sum(q[j] for j in sel))))
    return out


def _ticket_sets(ps, have, budget_yuan):
    """在「注数 = Prod k_i <= budget/2」下，最大化期望命中场数。

    期望命中 = 每场覆盖率之和，是【可分离】的 ⇒ 逐场 DP 就是精确解，
    不必枚举 3^14。每场只有三种选择，状态是「已买注数」。

    🔴 目标函数选「期望命中」而不是「期望奖金」：两者实测给出近乎相同的票，
       而期望奖金不可分离（依赖 m_c 的具体形状），DP 做不了。
    返回 (path, notes)；path[i] = (k, [场内序号...], 该场覆盖率)。
    """
    max_notes = int(budget_yuan // 2)
    Q = [ps[h[0]] for h in have]
    dp = {1: (0.0, [])}                      # 注数 -> (累计期望命中, 每场选择)
    for cand in [_ticket_cands(q) for q in Q]:
        nxt = {}
        for notes, (val, path) in dp.items():
            for k, sel, cov in cand:
                n = notes * k
                if n > max_notes:
                    continue
                v = val + cov
                if n not in nxt or v > nxt[n][0]:
                    nxt[n] = (v, path + [(k, sel, cov)])
        dp = nxt
    if not dp:
        return None
    notes, (_, path) = max(dp.items(), key=lambda kv: kv[1][0])
    return path, notes


def _ticket_prob_arr(Q, path):
    """票面内每个组合的概率，顺序与 itertools.product(*票面) 一致。

    🔴 不需要 all_probs() 的 3^14：票面本身就把每场限制在一个小集合里，外积即可。
    🔴 顺序陷阱（实测踩到过）：本函数【按 1 号场最显著】外积，即 product() 的顺序 ——
       而 all_probs() 是【14 号场最显著】（索引 = sum 3^k·d_k）。两边都是「同一批数、
       不同排列」，所以按均值算 ROI 时结果一样（相对差 <1e-5，纯求和顺序不同），
       但拿位置去对组合就会错得无声无息。这里刻意选 product() 顺序，好让
       票面字符串、组合列表、概率数组三者位置一一对应。
    """
    arr = np.array([Q[-1][j] for j in path[-1][1]], dtype=float)
    for k in range(len(path) - 2, -1, -1):
        arr = np.concatenate([arr * Q[k][j] for j in path[k][1]])
    return arr


def _ticket_ret(arr, Q, T, a, k_calib):
    """票面的期望回报率。arr = _ticket_prob_arr 的输出。

    🔴 分母是 m*k_calib + 1，不是 + 注数：票里每个组合互不相同，某一组合中奖时
       分钱的只有「彩民在该组合的票」+「我这一注」。用 + 注数会虚构出一份不存在的
       自稀释，而且会让 notes=1 退化不出 [3] 节那条已批准的判据 m < 0.448*P*T。
    🔴 单注成本 2 元已经内建在 0.448*T/(m+n) 里（T 是【票数】不是金额），
       不要再除一次 2 —— 除两次会把 -48% 印成 -74%。
    """
    S = float(np.prod([sum(x ** a for x in q) for q in Q]))     # S(a) 闭式
    m = (arr ** a / S) * T
    return float((arr * np.minimum(TIER1_SHARE * T / (m * k_calib + 1.0),
                                   CAP / 2.0)).mean()) - 1.0


def _ticket_face(path):
    return "/".join("".join(SYM[j] for j in sel) for _, sel, _ in path)


def _ticket_one(ps, have, recent, res, budget_yuan, T):
    got = _ticket_sets(ps, have, budget_yuan)
    if not got:
        return None
    path, notes = got
    if notes > TICKET_NOTES_MAX:
        return {"budget": int(budget_yuan), "error": f"注数 {notes:,} 超过上限 "
                f"{TICKET_NOTES_MAX:,}（DP 与 ROI 都是 O(注数)）"}
    Q = [ps[h[0]] for h in have]
    arr = _ticket_prob_arr(Q, path)
    covs = [c[2] for c in path]
    model = [_ticket_ret(arr, Q, T, a, 1.0) for a in TICKET_A_GRID]
    calib = [_ticket_ret(arr, Q, T, a, CALIB_K) for a in TICKET_A_GRID]
    out = {
        "budget": int(budget_yuan), "notes": notes, "cost": notes * 2,
        "face": _ticket_face(path),
        "expect_hits": round(sum(covs), 2),          # = 14 场覆盖率之和
        "p_all": float(np.prod(covs)),               # 全中概率
        "p_max": float(arr.max()),                   # 票面内最大单注概率
        # 🔴 保留 6 位不是强迫症：report 里这些数是按 `x*100:.1f` 印的，若在这里先
        #    round(...,4)，-0.7585338 会先变成 -0.7585，再印成 "-75.8"（.x5 取偶）；
        #    而 CLI 孪生用全精度印出 "-75.9"。同一个数是同一个数，印出两个就是
        #    「两个地方给出两个数」。6 位之下再撞上 .x5 平局点的概率可以忽略。
        "roi_model": [round(x, _ROI_DP) for x in model],
        "roi_calib": [round(x, _ROI_DP) for x in calib],
        "roi_model_lo": round(min(model), _ROI_DP), "roi_model_hi": round(max(model), _ROI_DP),
        "roi_calib_lo": round(min(calib), _ROI_DP), "roi_calib_hi": round(max(calib), _ROI_DP),
    }
    if res is not None:                              # 已开奖的期顺带对一次彩果
        out["actual_hits"] = sum(1 for i, c in enumerate(path) if res[i] in c[1])
    return out


def ticket(issue=None, budget=None):
    """复式单测算 —— 回答「这个预算买到什么票面、成本多少、ROI 多少」。

    🔴 只算钱，不给「建议买多少」。任何预算档的 ROI 都是负的，这是 H0-2。
    """
    _ensure_once()
    issue, info, rows, recent = _load(issue)
    if issue is None:
        return {"issue": None, "error": "库里没有任何一期胜负彩存档"}
    if not rows:
        return {"issue": issue, "error": f"期 {issue} 没有存档行"}
    have, miss, _bold, _sharp, ps, src = _odds_metrics(rows)
    if len(have) < 14:
        return {"issue": issue, "error": f"只有 {len(have)}/14 场有赔率"
                f"（体彩 {sum(1 for r in rows if r['had_h'])} 场 / "
                f"庄家 {sum(1 for r in rows if r['odds_home'])} 场，两套都不齐）—— "
                "缺一场就分不出组合，造不了票面（同 [3] 节门槛）",
                "odds_missing": [m["no"] for m in miss]}
    T, t_src = _estimate_T(info, recent)                      # 与 _best_roi 同一口径
    if T is None:
        return {"issue": issue, "error": "没有历史销量，估不出 T（本期总票数）"}

    if budget is None:
        budgets = list(TICKET_BUDGET_LADDER)
    else:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            return {"issue": issue, "error": f"budget 不是数字: {budget!r}"}
        if budget < 2:
            return {"issue": issue, "error": f"预算 {budget} 元买不到一注（单注 2 元）"}
        budgets = [budget]

    # 已开奖的期才对齐彩果；缺任何一场就是 None（别把「没开」当成「没中」）
    res = [IDX.get((r["cz_result"] or "").strip()) for r in rows]
    if any(x is None for x in res):
        res = None

    return {
        "issue": issue,
        "sale_end": str(info["sale_end"]) if info and info.get("sale_end") else None,
        "T": T, "T_source": t_src, "calib_k": CALIB_K, "a_grid": list(TICKET_A_GRID),
        "odds_coverage": len(have),
        "odds_src": src,          # "jc" = 体彩竞彩 / "pinnacle" = 庄家盘（体彩不齐时的回退）
        "tickets": [t for t in (_ticket_one(ps, have, recent, res, b, T)
                                for b in budgets) if t],
        # H0-2 的原文口径，让读者知道这一块在回答表里的哪一条
        "rule": "H0-2",
        "note": "任何预算档 ROI 全为负，这是 36% 抽水本身（H0-2）。本块只算钱，"
                "不产出选号建议（X-2）。",
        # 🔴 口径声明：这两个约定会让 ROI 与直觉不符，写死在返回值里防止被当成别的意思
        "basis": {
            "prize": "仅一等奖（0.448*T），不含二等奖（0.192*T）—— 与 [3] 节判据同口径",
            "dilution": "分母 = 彩民在该组合的票 m + 1（票内各组合互不相同）。"
                        "若按「+ 总注数」算会虚构一份不存在的自稀释，2048 元档会从 "
                        "-82.0% 变成 -88.2%",
        },
    }


def _metrics(issue, info, rows, recent):
    have, miss, bold, sharp, ps, src = _odds_metrics(rows)
    m = {
        "issue": issue,
        "sale_end": str(info["sale_end"]) if info and info.get("sale_end") else None,
        "vig": round(1 - PRIZE_RETURN, 4),
        "odds_coverage": len(have),
        "odds_src": src,
        "odds_missing": miss,
        "bold_count": len(bold),
        "bold": bold,
        "sharpest": sharp,
        "drawn_count": sum(1 for r in rows if (r["cz_result"] or "").strip() in IDX),
        "recent_sales": recent,
        "prize_return": PRIZE_RETURN,
    }
    # H0-3 的指标 t_consist = |报告用的 T × 2 / 销量 − 1|。
    # 🔴 右边直接写成 销量/2 的定义式，【不许经 _estimate_T】—— 经它就成了同义反复，
    #    什么都查不出来。这一条是【绊线】：谁换个写法碰坏了 T 的定义，它立刻报 FAIL。
    #    只有已开奖（销量已知）才判得了；赛前没有可比对的量，判 SKIP。
    m["t_consist"] = None
    if info and info.get("total_sale_amount"):
        t_used, _ = _estimate_T(info, recent)
        if t_used:
            m["t_consist"] = round(
                abs(t_used * 2.0 / float(info["total_sale_amount"]) - 1.0), 8)

    roi = _best_roi(ps, have, info, recent)
    m["best_roi_calib"] = roi["calib_roi"] if roi else None
    m["best_roi_model"] = roi["model_roi"] if roi else None
    m["roi"] = roi
    return m


# ── 判定 ────────────────────────────────────────────────────────────────
_OPS = {">=": operator.ge, "<=": operator.le, "==": operator.eq,
        ">": operator.gt, "<": operator.lt}

# SKIP 那句提示默认说的是【组合层】的前置条件（赔率 14/14，见 S1-1）—— [3]/[5] 都靠它。
# 但 H0-3 的前置条件是【销量】不是赔率，指错地方比不指更坏，所以单列出来。
_SKIP_HINT = {"t_consist": "销量未出 —— 赛前没有可比对的量（见 H0-3 原文）"}


def _judge(rule, metrics):
    metric = rule["metric"]
    if not metric:
        # 不自动判定：manual 要人工答，cannot 是「判不了」，info 只展示要求原文
        return "NOTE", None, None
    val = metrics.get(metric)
    if val is None:
        return "SKIP", None, _SKIP_HINT.get(metric, "指标算不出（前置条件未满足，见 S1-1）")
    thr = rule["threshold"]
    try:
        if rule["op"] == "in":
            hit = val in [t.strip() for t in str(thr).split(",")]
        else:
            hit = _OPS[rule["op"]](float(val), float(thr))
    except (KeyError, TypeError, ValueError) as e:
        return "SKIP", val, f"判据无法求值: {type(e).__name__}: {e}"
    # 🔴 polarity=-1 的行：条件【命中】才是坏消息（H0-1 的「抽水还是 36%」）。
    #    少了这一步，H0-1 会以 PASS 收场，摘要打出 "FAIL: []"，读起来像放行。
    passed = hit if int(rule["polarity"]) == 1 else (not hit)
    if passed:
        return "PASS", val, None
    return ("FAIL" if rule["kind"] == "hard_fail" else "WARN"), val, None


def list_rules():
    """只读规则表本身 —— 不跑任何计算。回答「要求是什么」。"""
    _ensure_once()
    db = SessionLocal()
    try:
        rows = db.execute(text("""SELECT seq, section, code, title, kind, polarity, metric, op,
                threshold, requirement, evidence, source, is_active,
                revision, changed_at, change_reason, approved_by
            FROM sfc_check_rules ORDER BY seq""")).mappings().all()
        return [dict(r) for r in rows]
    finally:
        db.close()


def check(issue=None):
    """规则表 + 本期实测指标 + 逐条判定。回答「这期过不过」。"""
    _ensure_once()
    issue, info, rows, recent = _load(issue)
    if issue is None:
        return {"issue": None, "error": "库里没有任何一期胜负彩存档"}
    if not rows:
        return {"issue": issue, "error": f"期 {issue} 没有存档行"}
    metrics = _metrics(issue, info, rows, recent)

    db = SessionLocal()
    try:
        rules = db.execute(text("""SELECT seq, section, code, title, kind, polarity, metric, op,
                threshold, requirement, evidence, source, is_active,
                revision, changed_at, change_reason, approved_by
            FROM sfc_check_rules WHERE is_active=1 ORDER BY seq""")).mappings().all()
    finally:
        db.close()

    out, fails, warns = [], [], []
    for r in rules:
        r = dict(r)
        status, val, reason = _judge(r, metrics)
        if status == "FAIL":
            fails.append(f"{r['code']} {r['title']}")
        elif status == "WARN":
            warns.append(f"{r['code']} {r['title']}")
        # 浮点指标留 4 位就够读（ROI 写成 -0.603758261759499 没意义）
        shown = round(val, 4) if isinstance(val, float) else val
        out.append({**r, "status": status, "metric_value": shown, "reason": reason})

    return {
        "issue": issue,
        "sale_end": metrics["sale_end"],
        "metrics": metrics,
        "rules": out,
        "summary": {
            "FAIL": fails, "WARN": warns,
            "PASS": [f"{r['code']} {r['title']}" for r in out if r["status"] == "PASS"],
            "SKIP": [f"{r['code']} {r['title']} ({r['reason']})" for r in out if r["status"] == "SKIP"],
            "NOTE": [r["code"] for r in out if r["status"] == "NOTE"],
        },
        # 结论一句话说死：后面几节都只是在 36% 里挪几个百分点
        "verdict": ("否决：抽水 36.00% 写死，本校验器不接受任何「本期可以赚钱」的结论。"
                    "下面逐条是把「为什么」摆出来。"),
    }
