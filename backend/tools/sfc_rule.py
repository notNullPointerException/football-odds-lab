"""胜负彩校验规则的【唯一写入口】—— 改结论必须带日期和原因，且先经用户同意。

用户 2026-09-29 立：「如果以后胜负14的这个结论有变化，你需要经过我的同意才能写到表中。
每次变更需要加上当前日期、为什么变更。」

用法:
    # 看表里现在是什么（已批准的结论）
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py list
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py show S3-1

    # 查代码里的 _RULES 与表哪里不一致 —— 不一致 = 改了代码但【没生效】在表上
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py diff

    # 变更流水（谁、哪天、为什么）
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py log [S3-1]

    # 🔴 改（必须带 reason 和 approved-by，否则拒绝执行）
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py change S3-2 \
        --set threshold=1.6 \
        --reason "2026-09-30 重跑 crowd_power.py，n=200 期，Poisson MLE a=1.61 CI[1.42,1.83]..." \
        --approved-by "用户 2026-09-30"

    # 🔴 新增一条规则（同样是唯一入口；比 change 更重，因为它改变「什么算不合格」）
    #    必填: section / title / kind / polarity，外加 reason / approved-by
    PYTHONPATH=. .venv/bin/python tools/sfc_rule.py add H0-3 \
        --set section=0 --set seq=25 --set title="..." \
        --set kind=hard_fail --set polarity=1 \
        --set metric=t_consist --set op="<=" --set threshold=0.02 \
        --set requirement="..." --set evidence="..." --set source="..." \
        --reason "..." --approved-by "用户 2026-09-30"

🔴 运行 change / add 之前，先确认用户已经同意。这个脚本拦不住你 —— 它能做的是让
   「改了必须有日期、有原因、有留痕」，以及让没走这条路的改动【根本不生效】。
   日期由代码取 now()，不接受手填。

🔴 光在 app/services/sfc_check_service.py 的 _RULES 里加一条【不会生效】（表非空时
   ensure_tables 一个字符都不改），而且 tools/sfc_rule.py diff 会把这条差异摆出来。
   新增规则的正规顺序：① 这里 add 写库 ② 同步把同一条加进代码 _RULES ③ diff 应为空。
"""
import sys

sys.path.insert(0, ".")

from app.services import sfc_check_service as S


def _fmt_rule(r, wide=False):
    line = f"  {r['code']:<6} {r['section']:<2} {r['kind']:<9} rev{r['revision']:<3} {r['title']}"
    if not wide:
        return line
    out = [line]
    if r.get("metric"):
        out.append(f"         判定: {r['metric']} {r['op']} {r['threshold']}"
                   f"  (polarity={r['polarity']}"
                   + ("  命中即否决" if r["polarity"] == -1 else "") + ")")
    out.append(f"         要求: {r['requirement']}")
    if r.get("evidence"):
        out.append(f"         证据: {r['evidence']}")
    out.append(f"         来源: {r['source'] or '-'}")
    if r["revision"] and int(r["revision"]) > 1 or r.get("change_reason"):
        out.append(f"         变更: {r['changed_at']}  rev{r['revision']}"
                   f"  经 {r['approved_by'] or '-'} 同意")
        out.append(f"               {r['change_reason'] or '-'}")
    return "\n".join(out)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    if cmd == "list":
        for r in S.list_rules():
            print(_fmt_rule(r))
        return

    if cmd == "show":
        code = args[1]
        r = next((x for x in S.list_rules() if x["code"] == code), None)
        if not r:
            print(f"没有 {code} 这条规则")
            return
        print(_fmt_rule(r, wide=True))

    elif cmd == "diff":
        d = S.diff_against_code()
        if not d:
            print("代码 _RULES 与表一致，没有未生效的改动。")
            return
        print("🔴 以下差异【没有生效】—— 表里仍是已批准的版本。")
        print("   要生效必须先拿到用户同意，再走 change 子命令。\n")
        for x in d:
            print(f"  {x['code']}  {x['status']}")
            for f in x["fields"]:
                if isinstance(f, dict):
                    print(f"       {f['field']}: 代码={str(f['code'])[:60]!r}")
                    print(f"       {' ' * len(f['field'])}  表  ={str(f['table'])[:60]!r}")
                else:
                    print(f"       {f}")

    elif cmd == "log":
        rows = S.changelog(args[1] if len(args) > 1 else None)
        if not rows:
            print("还没有任何变更（表从建立至今没改过）。")
            return
        for r in rows:
            print(f"  {r['changed_at']}  {r['code']}  rev{r['revision']}  {r['field']}")
            print(f"      {str(r['old_value'])[:70]!r} -> {str(r['new_value'])[:70]!r}")
            print(f"      原因: {r['change_reason']}")
            print(f"      经 {r['approved_by']} 同意")

    elif cmd in ("change", "add"):
        code = args[1]
        sets, reason, by = {}, None, None
        i = 2
        while i < len(args):
            a = args[i]
            if a.startswith("--set"):
                kv = (a.split("=", 1)[1] if "=" in a else args[i + 1])
                if "=" not in a:
                    i += 1
                k, v = kv.split("=", 1)
                sets[k.strip()] = v.strip()
            elif a == "--reason":
                reason = args[i + 1]; i += 1
            elif a.startswith("--reason="):
                reason = a.split("=", 1)[1]
            elif a == "--approved-by":
                by = args[i + 1]; i += 1
            elif a.startswith("--approved-by="):
                by = a.split("=", 1)[1]
            i += 1
        # 数字/布尔字段转类型，其余按字符串
        for k in list(sets):
            if k in ("polarity", "is_active"):
                sets[k] = int(sets[k])
            elif k == "seq":
                sets[k] = int(sets[k])
        # 🔴 add 会【新增一条门槛】= 改变「什么算不合格」，比 change 更重。
        #    它同样要 reason + approved_by，同样逐字段留痕，同样改代码不生效。
        #    两条路的区别只有：add 显式传 allow_create=True（服务层默认拒绝新增）。
        try:
            r = S.apply_change(code, sets, reason, by, allow_create=(cmd == "add"))
        except ValueError as e:
            print(f"🔴 拒绝执行：{e}")
            sys.exit(2)
        print(f"{'已新增' if cmd == 'add' else '已改'} {r['code']}  rev{r['revision']}  {r['changed_at']}")
        print(f"  经 {r['approved_by']} 同意")
        print(f"  原因: {r['reason']}")
        for f in r["fields"]:
            print(f"  {f['field']}: {str(f['old'])[:60]!r} -> {str(f['new'])[:60]!r}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
