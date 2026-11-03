"""
ISA K-QLD 비중관리 계산 모듈 (HANDOFF 3장 명세)

순수 계산만 담는다: 비중, 매달 투자금 배분, 60% 상단 되돌리기, 12월 정기 점검, 날짜.
시세 조회·메일·저장은 main.py / isa_inbox.py 에서 한다.
"""

import json
import math
import os
import re
from datetime import date, timedelta

import holidays

# ─── 설정 (사용자 확정값) ───────────────────────────────────────────────────────
CODES = ("458730", "418660", "0046A0")  # 표시 순서: K-SCHD, K-QLD, K-SGOV
KQLD = "418660"
SGOV = "0046A0"
SCHD = "458730"
TARGET_WEIGHTS = {SCHD: 0.40, KQLD: 0.50, SGOV: 0.10}
NAMES = {SCHD: "K-SCHD", KQLD: "K-QLD", SGOV: "K-SGOV"}
FULL_NAMES = {
    SCHD: "TIGER 미국배당다우존스",
    KQLD: "TIGER 미국나스닥100레버리지(합성)",
    SGOV: "TIGER 미국초단기국채",
}
KQLD_UPPER_LIMIT = 0.60       # 넘으면 바로 50%로 (신호와 무관)
ANNUAL_BAND = 0.05            # 12월: 목표에서 5%p 넘게 벗어난 종목만
DEFAULT_CONTRIBUTION = 2_000_000
ISA_ROOM_DEFAULT = {2026: 39_974_900}   # 올해 남은 납입한도 (holdings의 isa_room이 있으면 그 값 우선)
MANDATORY_END = date(2027, 12, 8)       # 의무가입 종료
PAYDAY_DAY = 21
# 회사 사정으로 월급날이 다르면 여기에 적는다. 예: {"2026-12": "2026-12-18"}
# 환경변수 PAYDAY_OVERRIDE(JSON)로도 덮어쓸 수 있다.
PAYDAY_OVERRIDE: dict[str, str] = {}

# 과거 신호 통계 (종합문서 4-3, 합성 1986~2026). "오늘의 한 줄"에 쓴다.
SIGNAL_STATS = {
    "1단계": "과거 1단계 이상 신호 28회: 1년 뒤 중앙값 +42%, 81%가 플러스(신호 뒤 추가 하락 중앙값 -11%)",
    "2단계": "과거 2단계 이상 신호 19회: 1년 뒤 중앙값 +48%, 68%가 플러스(추가 하락 중앙값 -17%)",
    "3단계": "과거 3단계 신호 11회: 1년 뒤 중앙값 +8%, 55%만 플러스(추가 하락 중앙값 -35%). 바닥이 아니었어요",
}


# ─── 날짜 (HANDOFF 3-7) ───────────────────────────────────────────────────────

def _kr_holidays(year: int):
    return holidays.KR(years=[year - 1, year, year + 1])


def _krx_holidays(year: int):
    # KRX 휴장일: 공휴일 + 노동절 + 12/31 연말휴장 등
    return holidays.financial_holidays("XKRX", years=[year - 1, year, year + 1])


def is_krx_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _krx_holidays(d.year)


def next_krx_trading_day(d: date) -> date:
    """d 다음(당일 제외) KRX 영업일."""
    n = d + timedelta(days=1)
    while not is_krx_trading_day(n):
        n += timedelta(days=1)
    return n


def first_krx_trading_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d if is_krx_trading_day(d) else next_krx_trading_day(d)


def _overrides() -> dict:
    o = dict(PAYDAY_OVERRIDE)
    try:
        o.update(json.loads(os.environ.get("PAYDAY_OVERRIDE", "") or "{}"))
    except json.JSONDecodeError:
        pass
    return o


def payday(year: int, month: int) -> date:
    """월급날 = 21일. 주말·공휴일이면 직전 평일."""
    key = f"{year:04d}-{month:02d}"
    o = _overrides()
    if key in o:
        return date.fromisoformat(o[key])
    d = date(year, month, PAYDAY_DAY)
    hol = _kr_holidays(year)
    while d.weekday() >= 5 or d in hol:
        d -= timedelta(days=1)
    return d


def invest_day(year: int, month: int) -> date:
    """투자일 = 월급날 다음 KRX 영업일."""
    return next_krx_trading_day(payday(year, month))


def _add_month(y: int, m: int, k: int) -> tuple[int, int]:
    i = y * 12 + (m - 1) + k
    return i // 12, i % 12 + 1


def next_invest_day(today: date) -> date:
    """오늘 이후(오늘 포함) 가장 가까운 투자일."""
    for k in range(0, 3):
        y, m = _add_month(today.year, today.month, k)
        d = invest_day(y, m)
        if d >= today:
            return d
    raise RuntimeError("투자일 계산 실패")


def last_invest_day(today: date) -> date:
    """오늘 이전(오늘 포함) 가장 최근 투자일."""
    for k in range(0, -3, -1):
        y, m = _add_month(today.year, today.month, k)
        d = invest_day(y, m)
        if d <= today:
            return d
    raise RuntimeError("투자일 계산 실패")


def december_check_start(today: date) -> date:
    """다가오는(또는 진행 중인) 12월 점검 시작일 = 12월 첫 영업일."""
    return first_krx_trading_day(today.year, 12)


def is_december_check(today: date) -> bool:
    return today.month == 12 and today >= first_krx_trading_day(today.year, 12)


# ─── 비중 (HANDOFF 3-2) ───────────────────────────────────────────────────────

def valuation(holdings: dict, prices: dict) -> dict | None:
    """총자산 = 3종목 평가액 + 기존 종목 평가액 + 예수금. 가격이 하나라도 없으면 None."""
    qty = {c: int(holdings.get("holdings", {}).get(c, 0) or 0) for c in CODES}
    values = {}
    for c in CODES:
        p = prices.get(c)
        if p is None or not (p > 0):
            return None
        values[c] = qty[c] * float(p)
    others = float(sum(float(o.get("value_krw", 0) or 0) for o in holdings.get("others", [])))
    cash = float(holdings.get("cash_krw", 0) or 0)
    total = sum(values.values()) + others + cash
    if total <= 0:
        return None
    return {
        "qty": qty, "values": values, "others": others, "cash": cash, "total": total,
        "weights": {c: values[c] / total for c in CODES},
    }


# ─── 투자금 배분 (HANDOFF 3-3) ─────────────────────────────────────────────────

def allocate(values: dict, prices: dict, funds: float, total_after: float,
             codes=CODES) -> dict:
    """목표보다 부족한 종목부터 funds를 나눈다 (1주 단위, 남는 돈으로 1주씩 추가).

    need[k] = max(0, 목표[k] × total_after − 평가액[k])
    sum(need) >= funds → 부족분 비례 배분, 아니면 need + 남는 돈 × 목표비중
    """
    codes = list(codes)
    need = {c: max(0.0, TARGET_WEIGHTS[c] * total_after - values[c]) for c in codes}
    s = sum(need.values())
    if s >= funds and s > 0:
        amt = {c: funds * need[c] / s for c in codes}
    else:
        tw = sum(TARGET_WEIGHTS[c] for c in codes)
        amt = {c: need[c] + (funds - s) * TARGET_WEIGHTS[c] / tw for c in codes}
    shares = {c: int(math.floor(amt[c] / prices[c] + 1e-9)) for c in codes}
    left = funds - sum(shares[c] * prices[c] for c in codes)
    # 남는 돈으로 살 수 있으면 need(남은 부족분)가 큰 순서대로 1주씩
    while True:
        afford = [c for c in codes if prices[c] <= left + 1e-9]
        if not afford:
            break
        c = max(afford, key=lambda k: amt[k] - shares[k] * prices[k])
        shares[c] += 1
        left -= prices[c]
    return {
        "amount": amt, "shares": shares,
        "cost": {c: shares[c] * prices[c] for c in codes},
        "leftover": left, "funds": funds,
    }


def monthly_plan(pf: dict, prices: dict, contribution: float) -> dict:
    plan = allocate(pf["values"], prices, contribution, pf["total"] + contribution)
    after = {c: pf["values"][c] + plan["cost"][c] for c in CODES}
    tot = pf["total"] + contribution
    plan["weights_after"] = {c: after[c] / tot for c in CODES}
    return plan


# ─── 주문 공통 ─────────────────────────────────────────────────────────────────

def _orders_to_targets(pf: dict, prices: dict, codes: list[str],
                       kqld_first_funding: bool) -> dict:
    """codes 안의 종목을 총자산 기준 목표비중으로 되돌리는 매도·매수 주 수.
    재원: 예수금 + 넘친 종목 매도. kqld_first_funding이면 K-QLD가 모자랄 때
    K-SGOV → K-SCHD 순으로 추가 매도해서 채운다(12월 점검 규칙)."""
    total, values, qty = pf["total"], dict(pf["values"]), pf["qty"]
    sells: dict[str, int] = {}
    for c in codes:
        excess = values[c] - TARGET_WEIGHTS[c] * total
        if excess > 0:
            n = min(qty[c], int(round(excess / prices[c])))
            if n > 0:
                sells[c] = n
                values[c] -= n * prices[c]
    funds = pf["cash"] + sum(n * prices[c] for c, n in sells.items())
    under = [c for c in codes if c not in sells and values[c] < TARGET_WEIGHTS[c] * total]

    buys: dict[str, int] = {}
    if kqld_first_funding and KQLD in under:
        need_k = TARGET_WEIGHTS[KQLD] * total - values[KQLD]
        for src in (SGOV, SCHD):           # 예수금 → K-SGOV → K-SCHD
            short = need_k - funds
            if short <= 0:
                break
            if src in under or src == KQLD:
                continue
            avail = qty[src] - sells.get(src, 0)
            n = min(avail, int(math.ceil(short / prices[src])))
            if n > 0:
                sells[src] = sells.get(src, 0) + n
                values[src] -= n * prices[src]
                funds += n * prices[src]
        n = int(math.floor(min(funds, need_k) / prices[KQLD]))
        if n > 0:
            buys[KQLD] = n
            funds -= n * prices[KQLD]
            values[KQLD] += n * prices[KQLD]
        under = [c for c in under if c != KQLD]

    leftover = funds
    if under and funds > 0:
        a = allocate(values, prices, funds, total, codes=under)
        # 재원 이상으로 사지 않도록 부족분만큼만(남는 돈은 예수금으로 둔다)
        for c in under:
            cap = max(0.0, TARGET_WEIGHTS[c] * total - values[c])
            n = min(a["shares"][c], int(math.floor((cap + prices[c] * 0.5) / prices[c])))
            if n > 0:
                buys[c] = n
                values[c] += n * prices[c]
        leftover = funds - sum(buys.get(c, 0) * prices[c] for c in under)

    after_w = {c: values[c] / total for c in CODES}
    shortfall = {c: TARGET_WEIGHTS[c] * total - values[c] for c in codes
                 if TARGET_WEIGHTS[c] * total - values[c] > prices[c]}
    return {"sells": sells, "buys": buys, "leftover": leftover,
            "weights_after": after_w, "shortfall": shortfall}


def upper_rebalance(pf: dict, prices: dict) -> dict | None:
    """HANDOFF 3-4: K-QLD 비중 > 60% 이면 3종목을 목표비중으로 (기존 종목 제외)."""
    if pf["weights"][KQLD] <= KQLD_UPPER_LIMIT:
        return None
    return _orders_to_targets(pf, prices, list(CODES), kqld_first_funding=False)


def december_check(pf: dict, prices: dict) -> dict:
    """HANDOFF 3-5: 목표에서 5%p 넘게 벗어난 종목만 목표로. K-QLD 부족분은 예수금 → K-SGOV → K-SCHD."""
    off = [c for c in CODES if abs(pf["weights"][c] - TARGET_WEIGHTS[c]) > ANNUAL_BAND]
    if not off:
        return {"off": [], "sells": {}, "buys": {}, "leftover": pf["cash"],
                "weights_after": dict(pf["weights"]), "shortfall": {}}
    r = _orders_to_targets(pf, prices, off, kqld_first_funding=True)
    r["off"] = off
    return r


# ─── 금액 문자열 ───────────────────────────────────────────────────────────────

def parse_amount(text: str) -> int | None:
    """'3500000', '3,500,000', '350만', '350만원', '1억' → 원 단위 정수."""
    t = text.replace(",", "").replace(" ", "").replace("원", "")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)만)?(\d+)?", t)
    if not m or not any(m.groups()):
        return None
    eok, man, won = m.groups()
    v = 0.0
    if eok:
        v += float(eok) * 1e8
    if man:
        v += float(man) * 1e4
    if won:
        if eok or man:
            v += float(won)
        else:
            v = float(won)
    return int(round(v)) if v > 0 else None


def contribution_for(holdings: dict | None, invest_d: date) -> int:
    """이번 투자일에 쓸 투자금: [투자금] 메일로 바꾼 값이 있으면 그 값."""
    if holdings:
        o = holdings.get("contrib_override") or {}
        if o.get("month") == invest_d.strftime("%Y-%m") and o.get("amount"):
            return int(o["amount"])
    return DEFAULT_CONTRIBUTION


def isa_room(holdings: dict | None, today: date) -> int | None:
    """올해 남은 ISA 납입한도. 모르면 None."""
    r = (holdings or {}).get("isa_room") or {}
    if r.get("year") == today.year and r.get("krw") is not None:
        return int(r["krw"])
    return ISA_ROOM_DEFAULT.get(today.year)
