"""
잔고 입력 (HANDOFF 4장): Gmail로 받은 캡처 → Gemini 인식 → 비공개 저장(Actions 변수 HOLDINGS_JSON)

명령 메일 (본인 주소 → 본인 주소). 제목 꼬리표([잔고] 등)는 있어도 없어도 된다.
- 캡처 이미지 첨부                     → Gemini가 나무증권 계좌잔고 화면인지 먼저 판단.
                                          맞으면 잔고 인식 후 확인 메일, 아니면 조용히 무시
                                          (제목에 [잔고]가 있는데 아니면 오류 메일)
- 한 줄에 'K-QLD 212' / '예수금 300000' → 수량 고치기 ([수정] 꼬리표는 선택)
- 한 줄에 '투자금 350만'                → 이번 투자일 투자금 변경 후 배분표 재발송

⚠️ 이 저장소는 공개 저장소이고 Actions 로그도 공개된다. 보유 수량·금액은 절대 print 하지 않는다.
"""

import base64
import email
import imaplib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import isa

KST = ZoneInfo("Asia/Seoul")
LOCAL_HOLDINGS = Path(__file__).parent / "holdings.json"   # 로컬 시험용 (.gitignore)
VAR_NAME = "HOLDINGS_JSON"
BOT_HEADER = "X-ISA-Bot"          # 서비스가 보낸 메일 표시 (명령으로 다시 읽지 않기 위해)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"   # Actions 변수 GEMINI_MODEL로 바꿀 수 있음


# ─── 보유 저장소 ───────────────────────────────────────────────────────────────

def load_holdings() -> dict | None:
    """1순위 환경변수 HOLDINGS_JSON(Actions 변수), 2순위 로컬 holdings.json."""
    raw = os.environ.get(VAR_NAME, "").strip()
    if not raw and LOCAL_HOLDINGS.exists():
        raw = LOCAL_HOLDINGS.read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("⚠️  HOLDINGS_JSON 형식 오류 (내용은 표시하지 않음)")
        return None
    return data if isinstance(data, dict) else None


def has_balance(h: dict | None) -> bool:
    """잔고 캡처가 한 번이라도 저장됐는지 (메일함 상태만 있는 경우는 False)."""
    return bool(h and h.get("updated") and isinstance(h.get("holdings"), dict))


def save_holdings(data: dict, dry_run: bool) -> bool:
    """Actions 변수 HOLDINGS_JSON을 고친다(시크릿 HOLDINGS_PAT 필요). 로컬/dry-run이면 파일로."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    token = os.environ.get("HOLDINGS_PAT", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if dry_run or not token or not repo:
        LOCAL_HOLDINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print("💾 holdings 저장: 로컬 파일" + (" (HOLDINGS_PAT 없음)" if not token and not dry_run else ""))
        return True
    os.environ[VAR_NAME] = body
    url = f"https://api.github.com/repos/{repo}/actions/variables"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
    payload = json.dumps({"name": VAR_NAME, "value": body}).encode()
    for method, u in (("PATCH", f"{url}/{VAR_NAME}"), ("POST", url)):
        try:
            urllib.request.urlopen(urllib.request.Request(u, data=payload, headers=headers, method=method), timeout=30)
            print("💾 holdings 저장: Actions 변수 HOLDINGS_JSON")
            return True
        except urllib.error.HTTPError as e:
            if not (method == "PATCH" and e.code == 404):
                print(f"❌ holdings 저장 실패: HTTP {e.code}")
                return False
    return False


# ─── Gemini 인식 ───────────────────────────────────────────────────────────────

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_balance_screen": {"type": "BOOLEAN", "description": "증권사 계좌잔고(보유 종목 표) 화면이면 true, 다른 사진이면 false"},
        "net_assets": {"type": "NUMBER", "description": "화면 상단의 순자산(원)"},
        "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING", "description": "종목명 그대로"},
            "qty": {"type": "NUMBER", "description": "잔고(보유 수량, 주)"},
            "value_krw": {"type": "NUMBER", "description": "평가금액(원)"},
            "avg_price": {"type": "NUMBER", "description": "매입가(원)"},
            "cur_price": {"type": "NUMBER", "description": "현재가(원)"},
        }, "required": ["name", "qty", "value_krw"]}},
    },
    "required": ["is_balance_screen", "net_assets", "items"],
}

_PROMPT = (
    "먼저 이 이미지가 NH 나무증권 '계좌잔고'(보유 종목 표) 화면인지 판단해서 is_balance_screen에 적으세요. "
    "다른 사진이면 false, net_assets 0, items 빈 배열로 답하세요. 맞으면:  이미지가 여러 장이면 같은 화면을 이어 찍은 것이니 "
    "겹치는 종목은 한 번만 넣으세요. 표의 각 종목은 2줄이에요: 종목명 | 평가손익·수익률 | "
    "잔고(수량)·평가금액 | 매입가·현재가. 숫자는 쉼표 없이 원 단위 숫자로, 화면에 보이는 값만 적으세요."
)


def gemini_read(images: list[tuple[str, bytes]]) -> dict:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY 시크릿이 없어요")
    parts = [{"text": _PROMPT}] + [
        {"inline_data": {"mime_type": mt, "data": base64.b64encode(b).decode()}} for mt, b in images]
    req = {"contents": [{"parts": parts}],
           "generationConfig": {"responseMimeType": "application/json", "responseSchema": _SCHEMA,
                                "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    r = urllib.request.Request(url, data=json.dumps(req).encode(), method="POST",
                               headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini HTTP {e.code}") from None
    text = out["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def map_code(name: str) -> str | None:
    n = name.replace(" ", "")
    if "나스닥100" in n and "레버" in n and "TIGER" in n.upper():
        return isa.KQLD
    if "배당다우존스" in n and "TIGER" in n.upper():
        return isa.SCHD
    if "초단기" in n and "국채" in n and "TIGER" in n.upper():
        return isa.SGOV
    return None


def to_holdings(parsed: dict, updated: str, prev: dict | None) -> tuple[dict, list[str]]:
    """Gemini 결과 → holdings 형식. 확인할 점(warnings)도 함께 돌려준다."""
    warns = []
    h = {c: 0 for c in isa.CODES}
    others = []
    total_val = 0.0
    for it in parsed.get("items", []):
        qty = int(round(float(it.get("qty", 0) or 0)))
        val = float(it.get("value_krw", 0) or 0)
        total_val += val
        cur = float(it.get("cur_price", 0) or 0)
        if cur > 0 and qty > 0 and abs(qty * cur - val) > max(0.02 * val, 2000):
            warns.append(f"{it.get('name')}: 수량×현재가({qty * cur:,.0f})와 평가금액({val:,.0f})이 달라요")
        code = map_code(str(it.get("name", "")))
        if code:
            h[code] += qty
        else:
            others.append({"name": str(it.get("name", "?")), "qty": qty, "value_krw": round(val)})
    net = float(parsed.get("net_assets", 0) or 0)
    cash = round(net - total_val)
    if cash < 0:
        warns.append(f"예수금이 음수({cash:,}원)로 계산돼요. 순자산이나 평가금액을 확인해 주세요")
        cash = 0
    data = dict(prev or {})
    data.update({"updated": updated, "cash_krw": cash, "holdings": h, "others": others,
                 "net_assets_read": round(net)})
    return data, warns


# ─── 메일 읽기 ─────────────────────────────────────────────────────────────────

def _dec(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _own_text(msg) -> str:
    """답장의 인용 부분(> 또는 '...작성:' 이후)을 뺀 본문 텍스트."""
    text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            break
    if not text:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                html = html.split('class="gmail_quote')[0]
                text = re.sub(r"<br\s*/?>|</div>|</p>", "\n", html)
                text = re.sub(r"<[^>]+>", "", text)
                break
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(">") or re.search(r"(작성:|wrote:)\s*$", s) or s.startswith("-----Original"):
            break
        lines.append(s)
    return "\n".join(lines)


_ALIASES = {"K-QLD": isa.KQLD, "KQLD": isa.KQLD, "K-SCHD": isa.SCHD, "KSCHD": isa.SCHD,
            "K-SGOV": isa.SGOV, "KSGOV": isa.SGOV, isa.KQLD: isa.KQLD, isa.SCHD: isa.SCHD, isa.SGOV: isa.SGOV}


_AMT = r"[\d,.]+\s*(?:억)?\s*(?:[\d,.]+\s*)?(?:만)?\s*원?"


def _pair(chunk: str):
    mm = re.fullmatch(r"\s*([A-Za-z0-9가-힣\-]+)\s*[:=]?\s*(" + _AMT + r")\s*(?:주)?\s*", chunk)
    if not mm:
        return None
    key, num = mm.group(1).upper(), mm.group(2)
    if key in ("예수금", "현금", "CASH"):
        v = isa.parse_amount(num)
        return ("cash", v) if v is not None else None
    if key in _ALIASES:
        try:
            return (_ALIASES[key], int(num.replace(",", "").replace("주", "").strip()))
        except ValueError:
            return None
    return None


def parse_fixes(text: str) -> list[tuple[str, int]]:
    """한 줄씩 본다. 'K-QLD 212, 예수금 300000' / '[수정] K-QLD 212' → [('418660', 212), ('cash', 300000)]
    꼬리표가 없으면 줄 전체가 '종목 수량' 형식일 때만 인정한다(일반 메모를 잘못 읽지 않도록)."""
    out = []
    for line in text.splitlines():
        m = re.match(r"\s*(?:\[수정\]|수정\s)\s*", line)
        tagged = bool(m)
        body = line[m.end():] if m else line
        chunks = [c for c in re.split(r"[,;/]", body) if c.strip()]
        pairs = [_pair(c) for c in chunks]
        if tagged:
            out += [p for p in pairs if p]
        elif chunks and all(pairs):
            out += pairs
    return out


def parse_contrib(text: str) -> int | None:
    """한 줄에 '투자금 350만' / '[투자금] 3500000' → 원 단위."""
    for line in text.splitlines():
        m = re.fullmatch(r"\s*\[?투자금\]?\s*[:=]?\s*(" + _AMT + r")\s*", line)
        if m:
            return isa.parse_amount(m.group(1)) or 0
    return None


def fetch_commands(state: dict) -> tuple[list[dict], dict]:
    """INBOX에서 본인이 보낸 새 명령 메일을 가져온다. state = {"uidvalidity", "last_uid"}"""
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pw:
        return [], state
    cmds = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(addr, pw)
        typ, data = M.select("INBOX", readonly=True)
        uidv = None
        typ2, resp = M.response("UIDVALIDITY")
        if resp and resp[0]:
            uidv = resp[0].decode() if isinstance(resp[0], bytes) else str(resp[0])
        last = int(state.get("last_uid", 0)) if state.get("uidvalidity") == uidv else 0
        if last:
            typ, data = M.uid("SEARCH", None, f"UID {last + 1}:*", "FROM", f'"{addr}"')
        else:
            since = (datetime.now(KST).date().toordinal() - 3)
            d = datetime.fromordinal(since).strftime("%d-%b-%Y")
            typ, data = M.uid("SEARCH", None, "SINCE", d, "FROM", f'"{addr}"')
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)
        for uid in uids:
            typ, md = M.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if not md or not md[0] or not isinstance(md[0], tuple):
                continue
            msg = email.message_from_bytes(md[0][1])
            last = max(last, uid)
            if msg.get(BOT_HEADER) or parseaddr(msg.get("From", ""))[1].lower() != addr.lower():
                continue
            subj = _dec(msg.get("Subject"))
            if subj.lstrip().startswith("[ISA"):
                continue  # 서비스가 보낸 메일
            try:
                when = parsedate_to_datetime(msg.get("Date")).astimezone(KST)
            except Exception:
                when = datetime.now(KST)
            body = _own_text(msg)
            images = [(p.get_content_type(), p.get_payload(decode=True)) for p in msg.walk()
                      if p.get_content_maintype() == "image" and p.get_payload(decode=True)]
            tagged = "[잔고]" in subj
            if tagged or images:
                cmds.append({"kind": "balance", "images": images, "when": when, "tagged": tagged})
            amt = parse_contrib(subj + "\n" + body)
            if amt is not None:
                cmds.append({"kind": "contrib", "amount": amt, "when": when})
            fixes = parse_fixes(subj + "\n" + body)
            if fixes:
                cmds.append({"kind": "fix", "fixes": fixes, "when": when})
        state = {"uidvalidity": uidv, "last_uid": last}
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return cmds, state


def apply_commands(holdings: dict | None, cmds: list[dict]) -> tuple[dict | None, list[dict]]:
    """명령을 순서대로 반영. 돌려주는 events는 메일로 알릴 내용."""
    events = []
    for c in cmds:
        when = c["when"]
        if c["kind"] == "balance":
            if not c["images"]:
                events.append({"kind": "error", "msg": "잔고 메일에 이미지가 없어요. 캡처를 첨부해 주세요."})
                continue
            try:
                parsed = gemini_read(c["images"])
            except Exception as e:
                events.append({"kind": "error", "msg": f"캡처 인식 실패: {e}"})
                continue
            if not parsed.get("is_balance_screen") or not parsed.get("items"):
                if c.get("tagged"):
                    events.append({"kind": "error", "msg": "보낸 이미지가 나무증권 계좌잔고 화면이 아닌 것 같아요. 보유 종목 표가 보이게 다시 캡처해 주세요."})
                continue  # 꼬리표 없는 다른 사진은 조용히 무시
            holdings, warns = to_holdings(parsed, when.strftime("%Y-%m-%d"), holdings)
            events.append({"kind": "balance", "warns": warns})
        elif c["kind"] == "fix":
            if not has_balance(holdings):
                events.append({"kind": "error", "msg": "아직 저장된 잔고가 없어서 [수정]을 반영하지 못했어요. 먼저 계좌잔고 캡처를 보내 주세요."})
                continue
            for code, v in c["fixes"]:
                if code == "cash":
                    holdings["cash_krw"] = v
                else:
                    holdings.setdefault("holdings", {})[code] = v
            holdings["updated"] = max(holdings.get("updated", ""), when.strftime("%Y-%m-%d"))
            events.append({"kind": "fix", "fixes": c["fixes"]})
        elif c["kind"] == "contrib":
            if not c["amount"]:
                events.append({"kind": "error", "msg": "투자금 금액을 읽지 못했어요. 예: 투자금 350만"})
                continue
            d = isa.next_invest_day(when.date())
            holdings = holdings or {}
            holdings["contrib_override"] = {"month": d.strftime("%Y-%m"), "amount": c["amount"]}
            events.append({"kind": "contrib", "amount": c["amount"], "invest_day": d})
    return holdings, events


def process_inbox(holdings: dict | None, dry_run: bool) -> tuple[dict | None, list[dict]]:
    """메일함을 확인해서 holdings를 갱신하고 저장한다. 실패해도 예외를 밖으로 내지 않는다."""
    if (os.environ.get("GITHUB_ACTIONS") and not dry_run
            and not (os.environ.get("HOLDINGS_PAT") and os.environ.get("GITHUB_REPOSITORY"))):
        print("ℹ️  HOLDINGS_PAT 시크릿이 없어 메일함 확인을 건너뜀 (저장할 곳이 없으면 같은 메일을 반복 처리하게 됨)")
        return holdings, []
    try:
        state = (holdings or {}).get("_inbox", {})
        cmds, new_state = fetch_commands(state)
        print(f"📬 명령 메일 {len(cmds)}건")
        if not cmds and new_state == state:
            return holdings, []
        holdings, events = apply_commands(holdings, cmds)
        holdings = holdings or {}
        holdings["_inbox"] = new_state
        save_holdings(holdings, dry_run)
        return holdings, events
    except Exception as e:
        print(f"⚠️  메일함 확인 실패: {type(e).__name__}")
        return holdings, [{"kind": "error", "msg": f"메일함 확인 실패: {type(e).__name__}"}]
