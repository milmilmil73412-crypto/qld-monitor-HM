"""HANDOFF 3장 검증 예시를 그대로 확인하는 시험. 실행: python test_isa.py"""

from datetime import date

import isa
from isa import KQLD, SCHD, SGOV

# 11/3 가격 (HANDOFF 3-3 예시 1: K-QLD 33,545 / K-SGOV 9,280 / K-SCHD 15,220원)
P = {KQLD: 33_545, SGOV: 9_280, SCHD: 15_220}
V19 = {SCHD: 8_200_000, KQLD: 9_000_000, SGOV: 1_800_000}


def approx(a, b, tol=1.0):
    assert abs(a - b) <= tol, (a, b)


def test_allocation_examples():
    # 예시 1: 1,900만 원, 투자금 200만 원
    a = isa.allocate(V19, P, 2_000_000, 21_000_000)
    approx(a["amount"][KQLD], 1_500_000); approx(a["amount"][SGOV], 300_000); approx(a["amount"][SCHD], 200_000)
    assert a["shares"] == {SCHD: 14, KQLD: 44, SGOV: 33}, a["shares"]   # 13+1, 44, 32+1
    approx(a["leftover"], 4_700)
    # 예시 2: 120만 원
    a = isa.allocate(V19, P, 1_200_000, 20_200_000)
    approx(a["amount"][KQLD], 1_000_000); approx(a["amount"][SGOV], 200_000); approx(a["amount"][SCHD], 0)
    # 예시 3: 350만 원
    a = isa.allocate(V19, P, 3_500_000, 22_500_000)
    approx(a["amount"][KQLD], 2_250_000); approx(a["amount"][SCHD], 800_000); approx(a["amount"][SGOV], 450_000)


CURRENT = {  # 2026-11-03 캡처 판독값
    "updated": "2026-11-04", "cash_krw": 264_533,
    "holdings": {KQLD: 210, SCHD: 0, SGOV: 0},
    "others": [{"name": "KODEX 미국나스닥100", "qty": 130, "value_krw": 3_220_100},
               {"name": "KODEX 미국S&P500", "qty": 109, "value_krw": 2_471_030},
               {"name": "SK하이닉스", "qty": 1, "value_krw": 1_414_000},
               {"name": "삼성전자", "qty": 4, "value_krw": 888_000},
               {"name": "우리금융지주", "qty": 1, "value_krw": 30_250}],
}


def test_current_account():
    pf = isa.valuation(CURRENT, P)
    approx(pf["total"], 15_332_363)
    approx(pf["weights"][KQLD] * 100, 45.9, 0.05)
    plan = isa.monthly_plan(pf, P, 2_000_000)
    approx(plan["amount"][SCHD], 1_347_800, 1_000)
    approx(plan["amount"][SGOV], 337_000, 1_000)
    approx(plan["amount"][KQLD], 315_000, 1_000)
    assert plan["leftover"] < min(P.values())
    assert sum(plan["cost"].values()) + plan["leftover"] == 2_000_000
    assert isa.upper_rebalance(pf, P) is None


def test_upper_rebalance():
    h = {"cash_krw": 0, "holdings": {KQLD: 300, SCHD: 300, SGOV: 50}, "others": []}
    pf = isa.valuation(h, P)
    assert pf["weights"][KQLD] > 0.60
    r = isa.upper_rebalance(pf, P)
    assert KQLD in r["sells"] and r["sells"][KQLD] > 0
    approx(r["weights_after"][KQLD], 0.50, 0.01)
    assert r["leftover"] >= 0
    # 59.9%는 그대로
    h2 = {"cash_krw": 0, "holdings": {KQLD: 1}, "others": [{"value_krw": 33_545 / 0.599 - 33_545}]}
    assert isa.upper_rebalance(isa.valuation(h2, P), P) is None


def test_december_check():
    # K-QLD 30%로 빠짐(K-SCHD 44%, K-SGOV 9%는 밴드 안) → 예수금 → K-SGOV 전부 → K-SCHD 매도로 50%까지
    h = {"cash_krw": 200_000, "holdings": {KQLD: 89, SCHD: 289, SGOV: 97},
         "others": [{"value_krw": 1_515_755}]}
    pf = isa.valuation(h, P)
    approx(pf["total"], 10_000_000)
    r = isa.december_check(pf, P)
    assert r["off"] == [KQLD], r["off"]
    assert r["sells"] == {SGOV: 97, SCHD: 61}, r["sells"]
    assert r["buys"] == {KQLD: 60}, r["buys"]
    approx(r["weights_after"][KQLD], 0.50, 0.005)
    assert r["leftover"] >= 0
    # K-SCHD가 5%p 넘게 초과면 K-SCHD를 팔아 K-QLD를 산다 (K-SGOV는 밴드 안이라 그대로)
    h = {"cash_krw": 100_000, "holdings": {KQLD: 90, SCHD: 400, SGOV: 100}, "others": []}
    r = isa.december_check(isa.valuation(h, P), P)
    assert SGOV not in r["sells"] and r["sells"].get(SCHD, 0) > 0 and r["buys"].get(KQLD, 0) > 0, r
    approx(r["weights_after"][KQLD], 0.50, 0.01)
    # 모두 ±5%p 안이면 할 일 없음
    h = {"cash_krw": 0, "holdings": {KQLD: 150, SCHD: 265, SGOV: 108}, "others": []}
    pf = isa.valuation(h, P)
    assert isa.december_check(pf, P)["off"] == []


def test_dates():
    assert isa.payday(2026, 11) == date(2026, 11, 20)
    assert isa.invest_day(2026, 11) == date(2026, 11, 23)
    assert isa.payday(2026, 12) == date(2026, 12, 21)
    assert isa.invest_day(2026, 12) == date(2026, 12, 22)
    assert isa.payday(2027, 2) == date(2027, 2, 19)
    assert isa.invest_day(2027, 2) == date(2027, 2, 22)
    assert isa.next_invest_day(date(2026, 11, 4)) == date(2026, 11, 23)
    assert isa.next_invest_day(date(2026, 11, 24)) == date(2026, 12, 22)
    assert isa.last_invest_day(date(2026, 11, 24)) == date(2026, 11, 23)
    assert not isa.is_krx_trading_day(date(2026, 12, 31))
    assert isa.december_check_start(date(2026, 11, 4)) == date(2026, 12, 1)


def test_parse_amount():
    assert isa.parse_amount("3500000") == 3_500_000
    assert isa.parse_amount("3,500,000원") == 3_500_000
    assert isa.parse_amount("350만") == 3_500_000
    assert isa.parse_amount("350만원") == 3_500_000
    assert isa.parse_amount("1억2000만") == 120_000_000
    assert isa.parse_amount("abc") is None


def test_inbox_parsing():
    import email as _email
    from datetime import datetime
    import isa_inbox as ib
    assert ib.parse_fixes("[수정] K-QLD 212") == [(KQLD, 212)]
    assert ib.parse_fixes("[수정] K-QLD 212, k-schd 10, 예수금 30만") == [(KQLD, 212), (SCHD, 10), ("cash", 300_000)]
    # 답장: 인용된 예시 문구는 무시
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["Subject"] = "me@gmail.com", "Re: [ISA] x"
    m.set_content("[수정] K-SGOV 33\n\n2026년 11월 4일 (수) 오전 7:00, <me@gmail.com>님이 작성:\n> [수정] K-QLD 212\n")
    body = ib._own_text(_email.message_from_bytes(m.as_bytes()))
    assert ib.parse_fixes(body) == [(SGOV, 33)], body
    # 캡처 인식 결과 → holdings
    parsed = {"net_assets": 15_332_363, "items": [
        {"name": "TIGER 미국나스닥100레버리지(합성)", "qty": 210, "value_krw": 7_044_450, "cur_price": 33_545},
        {"name": "KODEX 미국나스닥100", "qty": 130, "value_krw": 3_220_100},
        {"name": "KODEX 미국S&P500", "qty": 109, "value_krw": 2_471_030},
        {"name": "SK하이닉스", "qty": 1, "value_krw": 1_414_000},
        {"name": "삼성전자", "qty": 4, "value_krw": 888_000},
        {"name": "우리금융지주", "qty": 1, "value_krw": 30_250}]}
    h, warns = ib.to_holdings(parsed, "2026-11-04", {"_inbox": {"last_uid": 5}})
    assert h["holdings"] == {SCHD: 0, KQLD: 210, SGOV: 0} and h["cash_krw"] == 264_533 and not warns, (h, warns)
    assert len(h["others"]) == 5 and h["_inbox"] == {"last_uid": 5}
    assert ib.map_code("TIGER 미국초단기(3개월이하)국채") == SGOV and ib.map_code("TIGER 미국배당다우존스") == SCHD
    # 명령 반영: 수정 / 투자금
    when = datetime(2026, 11, 21, 9, 0, tzinfo=ib.KST)
    h2, ev = ib.apply_commands(h, [{"kind": "fix", "fixes": [(KQLD, 212)], "when": when},
                                   {"kind": "contrib", "amount": 3_500_000, "when": when}])
    assert h2["holdings"][KQLD] == 212 and h2["contrib_override"] == {"month": "2026-11", "amount": 3_500_000}
    assert isa.contribution_for(h2, date(2026, 11, 23)) == 3_500_000
    assert isa.contribution_for(h2, date(2026, 12, 22)) == isa.DEFAULT_CONTRIBUTION
    # 잔고가 없으면 [수정]은 오류로 안내
    _, ev = ib.apply_commands(None, [{"kind": "fix", "fixes": [(KQLD, 1)], "when": when}])
    assert ev[0]["kind"] == "error"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✅", name)
