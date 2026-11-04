"""
ISA 메일 본문 (HANDOFF 5장): 데일리 / 월급날 저녁 / 토요일 주간 / 잔고 확인

ctx 키 (main.py에서 만든다)
  today(date), holdings(dict|None), has_balance(bool), pf(valuation|None), prices, prev_prices, week_prices,
  qld_status, k_status(dict|None), combined(dict|None), market{ndx_from_high, usdkrw, kqld_from_high}
"""

from datetime import date, timedelta

import isa
from isa import CODES, NAMES, TARGET_WEIGHTS, KQLD

WD = ["월", "화", "수", "목", "금", "토", "일"]
SIGNAL_LEVELS = ("1단계", "2단계", "3단계")

# ─── 공통 조각 ─────────────────────────────────────────────────────────────────
C_TXT, C_SUB, C_DIM, C_LINE, C_CARD = "#e2e8f0", "#94a3b8", "#64748b", "#1e293b", "#111827"


def d_str(d: date) -> str:
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def won(v: float) -> str:
    return f"{v:,.0f}원"


def card(title: str, inner: str, border: str = C_LINE) -> str:
    return (f'<div style="margin:12px 0;padding:16px;border-radius:12px;background:{C_CARD};border:1px solid {border};">'
            f'<div style="color:{C_SUB};font-size:13px;font-weight:600;margin-bottom:8px;">{title}</div>'
            f'<div style="color:{C_TXT};font-size:14px;line-height:1.7;">{inner}</div></div>')


def table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f'<th style="text-align:{"left" if i == 0 else "right"};white-space:nowrap;padding:4px 6px;color:{C_DIM};font-weight:600;font-size:12px;">{h}</th>'
                 for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(
        f'<td style="text-align:{"left" if i == 0 else "right"};padding:4px 6px;border-top:1px solid {C_LINE};{"" if i == 0 and len(str(v)) > 8 else "white-space:nowrap;"}">{v}</td>'
        for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f'<table style="width:100%;border-collapse:collapse;font-size:13px;color:{C_TXT};"><tr>{th}</tr>{trs}</table>'


def page(title: str, sub: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0a0e17;font-family:'Segoe UI','Apple SD Gothic Neo',Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:20px;">
<div style="text-align:center;padding:16px 0;border-bottom:1px solid {C_LINE};">
  <h1 style="color:{C_TXT};font-size:20px;margin:0;">{title}</h1>
  <p style="color:{C_DIM};font-size:13px;margin:5px 0 0;">{sub}</p>
</div>
{body}
<div style="text-align:center;padding:16px 0;border-top:1px solid {C_LINE};margin-top:20px;">
  <p style="color:#475569;font-size:11px;margin:0;">자동 생성 메일이에요. 규칙: K-SCHD 40 / K-QLD 50 / K-SGOV 10, "50%로 되돌리기만". 투자 판단의 책임은 본인에게 있어요.</p>
</div></div></body></html>"""


def _sig(s: dict | None) -> str:
    return f"{s['emoji']}{s['name']}" if s else "?"


# ─── 할 일 판정 ────────────────────────────────────────────────────────────────

def plan_table(plan: dict, prices: dict) -> str:
    rows = [[NAMES[c], f"{plan['shares'][c]}주", won(plan["cost"][c]), f"{prices[c]:,.0f}원",
             f"{plan['weights_after'][c] * 100:.1f}%"] for c in CODES]
    return (table(["종목", "주 수", "금액", "1주 가격", "산 뒤 비중"], rows)
            + f'<div style="margin-top:6px;font-size:13px;color:{C_SUB};">남는 돈 {won(plan["leftover"])} · 투자금 {won(plan["funds"])}</div>')


def orders_table(r: dict, prices: dict) -> str:
    rows = []
    for c in CODES:
        if r["sells"].get(c):
            rows.append([NAMES[c], f'<span style="color:#ff6b6b">매도 {r["sells"][c]}주</span>', won(r["sells"][c] * prices[c]),
                         f"{r['weights_after'][c] * 100:.1f}%"])
        if r["buys"].get(c):
            rows.append([NAMES[c], f'<span style="color:#00d68f">매수 {r["buys"][c]}주</span>', won(r["buys"][c] * prices[c]),
                         f"{r['weights_after'][c] * 100:.1f}%"])
    t = table(["종목", "주문", "금액", "주문 뒤 비중"], rows) if rows else "주문 없음"
    extra = f'<div style="margin-top:6px;font-size:13px;color:{C_SUB};">주문 뒤 예수금 약 {won(r["leftover"])}'
    if r.get("shortfall"):
        extra += " · 아직 모자람: " + ", ".join(f"{NAMES[c]} {won(v)}" for c, v in r["shortfall"].items())
        extra += " (기존 종목을 정리하거나 투자금으로 채워요)"
    return t + extra + "</div>"


def todos(ctx: dict) -> list[dict]:
    """오늘 할 일 목록. 우선순위 순서: ⚖️ 60% 초과 > 💰 투자일 > 📅 12월 점검 > 📷 캡처."""
    today, h, pf, prices = ctx["today"], ctx["holdings"], ctx["pf"], ctx["prices"]
    out = []
    if not ctx["has_balance"]:
        out.append({"mark": "📷", "label": "잔고 캡처 보내기",
                    "html": "아직 저장된 잔고가 없어요. 나무증권 계좌잔고 화면을 캡처해서 본인 Gmail로 보내 주세요(제목은 없어도 돼요)."})
        return out
    if pf is None:
        out.append({"mark": "⚠️", "label": "시세 확인 실패",
                    "html": "오늘은 3종목 시세를 다 가져오지 못해서 비중을 계산하지 못했어요."})
        return out
    up = isa.upper_rebalance(pf, prices)
    if up:
        out.append({"mark": "⚖️", "label": "60% 초과: 50%로 되돌리기",
                    "html": f"K-QLD가 <b>{pf['weights'][KQLD] * 100:.1f}%</b>로 60%를 넘었어요. 신호와 상관없이 오늘 목표비중으로 되돌려요."
                            + orders_table(up, prices)})
    inv = isa.next_invest_day(today)
    if inv == today:
        amt = isa.contribution_for(h, inv)
        plan = isa.monthly_plan(pf, prices, amt)
        out.append({"mark": "💰", "label": "투자일: 오늘 매수",
                    "html": "오늘은 투자일이에요. 목표보다 부족한 종목부터 사요." + plan_table(plan, prices)
                            + _room_warning(h, today, amt)})
    if isa.is_december_check(today):
        dec = isa.december_check(pf, prices)
        if dec["off"]:
            out.append({"mark": "📅", "label": "12월 정기 점검",
                        "html": "목표에서 5%p 넘게 벗어난 종목: " + ", ".join(
                            f"{NAMES[c]} {pf['weights'][c] * 100:.1f}%(목표 {TARGET_WEIGHTS[c] * 100:.0f}%)" for c in dec["off"])
                                + ". K-QLD가 모자라면 예수금 → K-SGOV → K-SCHD 순으로 써요. 기존 종목은 팔지 않아요."
                                + orders_table(dec, prices)})
    last = isa.last_invest_day(today)
    if last < today and (h.get("updated") or "") < last.isoformat():
        out.append({"mark": "📷", "label": "잔고 캡처 필요",
                    "html": f"투자일({d_str(last)})이 지났는데 새 잔고 캡처가 아직 없어요(마지막 {h.get('updated', '?')}). "
                            "매수했으면 계좌잔고 화면을 캡처해서 본인 Gmail로 보내 주세요."})
    return out


def _room_warning(h, today, amount) -> str:
    room = isa.isa_room(h, today)
    if room is not None and amount > room:
        return (f'<div style="margin-top:8px;color:#ffaa00;font-size:13px;">⚠️ 투자금 {won(amount)}이 올해 남은 ISA 납입한도 '
                f'{won(room)}보다 커요. 넘는 돈은 ISA에 넣을 수 없어요.</div>')
    if room is not None:
        return f'<div style="margin-top:6px;color:{C_DIM};font-size:12px;">올해 남은 ISA 납입한도(설정값): {won(room)}</div>'
    return ""


# ─── 데일리 ───────────────────────────────────────────────────────────────────

def _account_card(ctx: dict) -> str:
    pf, prices, prev = ctx["pf"], ctx["prices"], ctx.get("prev_prices") or {}
    if not pf:
        return ""
    w = pf["weights"]
    rows = [[NAMES[c], f"{pf['qty'][c]}주", won(pf["values"][c]), f"{w[c] * 100:.1f}%", f"{TARGET_WEIGHTS[c] * 100:.0f}%"] for c in CODES]
    rows.append(["기존 종목(정리 대상)", "", won(pf["others"]), f"{pf['others'] / pf['total'] * 100:.1f}%", "0%"])
    rows.append(["예수금", "", won(pf["cash"]), f"{pf['cash'] / pf['total'] * 100:.1f}%", ""])
    chg = ""
    if all(prev.get(c) for c in CODES):
        d = sum(pf["qty"][c] * (prices[c] - prev[c]) for c in CODES)
        chg = f" · 3종목 전일 대비 <b style='color:{'#00d68f' if d >= 0 else '#ff6b6b'}'>{d:+,.0f}원</b>"
    v = pf["values"][KQLD]
    gap = (isa.KQLD_UPPER_LIMIT - w[KQLD]) * 100
    if gap > 0 and v > 0:
        x = (isa.KQLD_UPPER_LIMIT * pf["total"] - v) / ((1 - isa.KQLD_UPPER_LIMIT) * v) * 100
        dist = f"60%까지 {gap:.1f}%p 남음 (K-QLD가 약 +{x:.0f}% 오르면 60%)"
    elif gap > 0:
        dist = f"60%까지 {gap:.1f}%p 남음"
    else:
        dist = f"<b style='color:#ff6b6b'>60%를 {-gap:.1f}%p 넘음</b>"
    head = f"총액 <b>{won(pf['total'])}</b>{chg}<br>K-QLD {w[KQLD] * 100:.1f}% · {dist}"
    return card(f"2. 내 계좌 (잔고 {ctx['holdings'].get('updated', '?')} 기준 수량 × 최근 종가)", head + table(["종목", "수량", "평가액", "비중", "목표"], rows))


def _market_card(ctx: dict) -> str:
    m, q, k, comb = ctx["market"], ctx["qld_status"], ctx.get("k_status"), ctx.get("combined")
    parts = []
    if m.get("ndx_from_high") is not None:
        parts.append(f"나스닥100 52주 고점 대비 <b>{m['ndx_from_high']:+.1f}%</b>")
    if m.get("usdkrw"):
        parts.append(f"원/달러 <b>{m['usdkrw']:,.1f}</b>")
    if m.get("kqld_from_high") is not None:
        parts.append(f"K-QLD 52주 고점 대비 <b>{m['kqld_from_high']:+.1f}%</b>")
    sig = f"QLD(달러) {_sig(q)} · K-QLD(원화) {_sig(k)}"
    if comb:
        sig += f" → 종합 {comb['emoji']} {comb['label']}"
    return card("3. 시장 (신호는 참고용, 매수량은 바꾸지 않아요)", "<br>".join(parts + [sig]))


def _schedule_card(today: date) -> str:
    inv = isa.next_invest_day(today)
    pay = isa.payday(inv.year, inv.month)
    lines = [f"다음 투자일 {d_str(inv)} <b>D-{(inv - today).days}</b> (월급날 {d_str(pay)})"]
    if isa.is_december_check(today):
        lines.append("12월 정기 점검 <b>진행 중</b>")
    else:
        dc = isa.december_check_start(today)
        lines.append(f"12월 점검 {d_str(dc)} <b>D-{(dc - today).days}</b>")
    if today <= isa.MANDATORY_END:
        lines.append(f"ISA 의무가입 종료 {isa.MANDATORY_END.isoformat()} <b>D-{(isa.MANDATORY_END - today).days}</b>")
    return card("4. 다음 일정", "<br>".join(lines))


def one_liner(ctx: dict) -> str:
    names = [s["name"] for s in (ctx["qld_status"], ctx.get("k_status")) if s]
    lv = [n for n in SIGNAL_LEVELS if n in names]
    if lv:
        deepest = lv[-1]
        return ("규칙상 할 일 없음. 떨어졌다고 50% 넘게 더 사지 않아요(떨어지면 K-QLD 비중이 줄어서, 투자일에 저절로 더 사게 돼요). "
                f"참고: {isa.SIGNAL_STATS[deepest]}.")
    if "관심" in names:
        return "조정 구간 근처예요. 규칙은 그대로: 투자일에 부족한 종목부터 사요."
    return "평소대로예요. 투자일에만 부족한 종목부터 사면 돼요."


def daily_subject(ctx: dict, todo: list[dict], kind: str = "데일리") -> str:
    if todo:
        mark, label = todo[0]["mark"], todo[0]["label"]
        if len(todo) > 1:
            label += f" 외 {len(todo) - 1}건"
    else:
        mark, label = "✅", "할 일 없음"
    pf = ctx["pf"]
    w = f"{pf['weights'][KQLD] * 100:.1f}%" if pf else "?%"
    return f"[ISA {kind}] {mark} {label} · K-QLD {w} · QLD {_sig(ctx['qld_status'])} / K-QLD {_sig(ctx.get('k_status'))}"


def build_isa_section(ctx: dict, todo: list[dict]) -> str:
    """데일리 메일 위쪽(1~5번). 아래에는 기존 신호 상세 카드가 이어진다."""
    if todo:
        t = "".join(f'<div style="margin-bottom:10px;"><b>{x["mark"]} {x["label"]}</b><br>{x["html"]}</div>' for x in todo)
    else:
        t = "✅ 오늘은 할 일이 없어요."
    return (card("1. 오늘 할 일", t, border="#3b82f6")
            + _account_card(ctx) + _market_card(ctx) + _schedule_card(ctx["today"])
            + card("5. 오늘의 한 줄", one_liner(ctx)))


# ─── 월급날 저녁 / 투자금 변경 ────────────────────────────────────────────────

def payday_email(ctx: dict, amount: int, invest_d: date, changed: bool = False) -> tuple[str, str]:
    pf, prices, h, today = ctx["pf"], ctx["prices"], ctx["holdings"], ctx["today"]
    kind = "투자금 변경" if changed else "월급날"
    when = "내일" if (invest_d - today).days == 1 else d_str(invest_d)
    if not pf:
        body = card("배분표를 만들지 못했어요",
                    "저장된 잔고가 없거나 시세를 가져오지 못했어요. 계좌잔고 캡처를 본인 Gmail로 보내 주세요.")
        return f"[ISA {kind}] ⚠️ {when} 살 것 계산 실패", page(f"💰 {when} 살 것", d_str(invest_d), body)
    plan = isa.monthly_plan(pf, prices, amount)
    stale = ""
    last = isa.last_invest_day(today)
    if (h.get("updated") or "") < last.isoformat():
        stale = f'<div style="margin-top:8px;color:#ffaa00;font-size:13px;">⚠️ 잔고가 {h.get("updated", "?")} 기준이에요. 지난 투자일({d_str(last)}) 뒤 캡처를 보내면 더 정확해요.</div>'
    body = (card(f"💰 {d_str(invest_d)} 투자일에 살 것 (투자금 {won(amount)})",
                 "목표(40/50/10)보다 부족한 종목부터 1주 단위로 나눴어요." + plan_table(plan, prices)
                 + _room_warning(h, today, amount) + stale, border="#00d68f")
            + card("금액이 다르면", "본인 Gmail로 <b>투자금 350만</b> (또는 3500000) 한 줄만 써서 보내면 다시 계산해서 보내 드려요."
                   " 가격은 오늘 종가 기준이라, 내일 실제 가격과 조금 다를 수 있어요."))
    subj = f"[ISA {kind}] 💰 {when} 살 것 · " + " / ".join(f"{NAMES[c]} {plan['shares'][c]}주" for c in CODES)
    return subj, page(f"💰 {when} 살 것", f"투자일 {d_str(invest_d)} · 계산 {d_str(today)}", body)


# ─── 토요일 주간 요약 ─────────────────────────────────────────────────────────

def weekly_email(ctx: dict, todo: list[dict]) -> tuple[str, str]:
    """(제목, 위쪽 본문). 아래에는 기존 신호 상세 카드가 이어진다."""
    pf, prices, wk, today = ctx["pf"], ctx["prices"], ctx.get("week_prices") or {}, ctx["today"]
    parts = []
    head = "주간"
    if pf and all(wk.get(c) for c in CODES):
        before = {c: pf["qty"][c] * wk[c] for c in CODES}
        now = pf["values"]
        d = sum(now.values()) - sum(before.values())
        base = sum(before.values())
        pct = d / base * 100 if base else 0.0
        tot_before = pf["total"] - d
        rows = [[NAMES[c], f"{wk[c]:,.0f} → {prices[c]:,.0f}", f"{(prices[c] / wk[c] - 1) * 100:+.1f}%",
                 f"{before[c] / tot_before * 100:.1f}% → {pf['weights'][c] * 100:.1f}%"] for c in CODES]
        parts.append(card("이번 주 수익",
                          f"3종목 평가액 {d:+,.0f}원 ({pct:+.2f}%) · 총액 {won(pf['total'])}<br>"
                          f"<span style='color:{C_DIM};font-size:12px;'>수량은 잔고 {ctx['holdings'].get('updated', '?')} 기준으로 고정해서 계산했어요.</span>"
                          + table(["종목", "가격 1주 전→지금", "주간", "비중 변화"], rows)))
        head = f"주간 {pct:+.1f}%"
    elif not ctx["has_balance"]:
        parts.append(card("이번 주 수익", "저장된 잔고가 없어서 계산하지 못했어요. 계좌잔고 캡처를 본인 Gmail로 보내 주세요."))
    if todo:
        parts.append(card("할 일", "".join(f'<div style="margin-bottom:10px;"><b>{x["mark"]} {x["label"]}</b><br>{x["html"]}</div>' for x in todo), border="#3b82f6"))
    # 다음 주 일정 (다음 월~일)
    mon = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    sun = mon + timedelta(days=6)
    ev = []
    inv = isa.next_invest_day(mon)
    if inv <= sun:
        pay = isa.payday(inv.year, inv.month)
        if mon <= pay <= sun:
            ev.append(f"{d_str(pay)} 월급날 (저녁에 배분표 메일)")
        ev.append(f"{d_str(inv)} <b>투자일</b>")
    dc = isa.december_check_start(mon)
    if mon <= dc <= sun:
        ev.append(f"{d_str(dc)} 12월 정기 점검 시작")
    if mon <= isa.MANDATORY_END <= sun:
        ev.append(f"{d_str(isa.MANDATORY_END)} ISA 의무가입 종료")
    parts.append(card(f"다음 주 일정 ({d_str(mon)}~{d_str(sun)})", "<br>".join(ev) if ev else "정해진 일정 없음"))
    parts.append(_market_card(ctx))
    parts.append(card("한 줄", one_liner(ctx)))
    w = f"{pf['weights'][KQLD] * 100:.1f}%" if pf else "?%"
    mark = todo[0]["mark"] + " " if todo else ""
    subj = f"[ISA 주간] {mark}{head} · K-QLD {w} · QLD {_sig(ctx['qld_status'])} / K-QLD {_sig(ctx.get('k_status'))}"
    return subj, "".join(parts)


# ─── 잔고 입력 결과 ───────────────────────────────────────────────────────────

FIX_HELP = ("틀린 곳이 있으면 이 메일에 <b>답장</b>으로 <code>K-QLD 212</code> 처럼 한 줄로 보내 주세요. "
            "여러 개는 쉼표로: <code>K-QLD 212, K-SCHD 10</code> · 예수금은 <code>예수금 300000</code>")


def confirm_email(h: dict, ev: dict, pf: dict | None) -> tuple[str, str]:
    rows = []
    for c in CODES:
        q = h["holdings"].get(c, 0)
        rows.append([f"{NAMES[c]} ({c})", f"{q}주", won(pf["values"][c]) if pf else "-"])
    for o in h.get("others", []):
        rows.append([f"{o.get('name')} (기존)", f"{o.get('qty', '')}주", won(o.get("value_krw", 0))])
    rows.append(["예수금", "", won(h.get("cash_krw", 0))])
    total = f"<br>순자산(화면) {won(h['net_assets_read'])}" if h.get("net_assets_read") else ""
    if pf:
        total += f" · 계산 총액 {won(pf['total'])} · K-QLD {pf['weights'][KQLD] * 100:.1f}%"
    warns = "".join(f"<div style='color:#ffaa00;font-size:13px;'>⚠️ {w}</div>" for w in ev.get("warns", []))
    title = "✏️ 수정 반영" if ev["kind"] == "fix" else "📷 잔고 인식 결과"
    body = (card(f"{title} (잔고 {h.get('updated', '?')} 기준)", table(["종목", "수량", "평가액(최근 종가)"], rows) + total + warns,
                 border="#ffaa00" if ev.get("warns") else C_LINE)
            + card("틀리면", FIX_HELP)
            + card("개인정보", "캡처는 Google Gemini로 읽어요. 무료 등급은 입력이 구글 서비스 개선에 쓰일 수 있으니, 계좌번호는 가리고 보내 주세요."))
    subj = f"[ISA] {title} · " + " / ".join(f"{NAMES[c]} {h['holdings'].get(c, 0)}주" for c in CODES)
    if ev.get("warns"):
        subj += " · ⚠️ 확인 필요"
    return subj, page(title, f"잔고 {h.get('updated', '?')}", body)


def error_email(msg: str) -> tuple[str, str]:
    return "[ISA] ⚠️ 잔고 입력 문제", page("⚠️ 잔고 입력 문제", "", card("내용", msg) + card("다시 보내는 방법", "계좌잔고 캡처를 첨부해서 본인 Gmail로 보내 주세요. 제목은 없어도 돼요."))
