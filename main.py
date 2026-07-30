#!/usr/bin/env python3
"""
QLD 자동 매수 알림 및 웹 대시보드 시스템
==========================================
- yfinance 기반 QLD 지표 계산 (200일선 괴리율, 주봉 RSI)
- 상태 판정 (안정/관심/1단계/2단계/3단계)
- Gmail SMTP 이메일 발송
- index.html 대시보드 생성
- data/history.json 이력 관리
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


# ─── 상수 ──────────────────────────────────────────────────────────────────────

KST = ZoneInfo("Asia/Seoul")
EST = ZoneInfo("US/Eastern")

# 상태 매트릭스 정의
STATUS_LEVELS = [
    {
        "name": "3단계",
        "emoji": "🟣",
        "color": "#b86bff",
        "description": "역사적 바닥 — 2차 8주 + 배당금 전력 매수",
        "rsi_threshold": 36,
        "deviation_threshold": -26,
    },
    {
        "name": "2단계",
        "emoji": "🔴",
        "color": "#ff3860",
        "description": "매력적 매수 — 계획 철저 이행",
        "rsi_threshold": 42,
        "deviation_threshold": -18,
    },
    {
        "name": "1단계",
        "emoji": "🟠",
        "color": "#ff6b35",
        "description": "분할 매수 시작 — 8주 1차 구동",
        "rsi_threshold": 48,
        "deviation_threshold": -10,
    },
]

STATUS_WATCH = {
    "name": "관심",
    "emoji": "🟡",
    "color": "#ffaa00",
    "description": "매수 임계치 근접 — 현금 사전 점검",
}

STATUS_STABLE = {
    "name": "안정",
    "emoji": "🟢",
    "color": "#00d68f",
    "description": "평시 관망 구간",
}

HISTORY_MAX_DAYS = 90
HISTORY_PATH = Path(__file__).parent / "data" / "history.json"
OUTPUT_HTML_PATH = Path(__file__).parent / "index.html"


# ─── 지표 계산 ─────────────────────────────────────────────────────────────────

def fetch_qld_data(period_days: int = 300) -> pd.DataFrame:
    """yfinance에서 QLD 일봉 데이터를 가져온다."""
    ticker = yf.Ticker("QLD")
    # 200일선 계산을 위해 충분한 데이터 확보
    df = ticker.history(period=f"{period_days}d", interval="1d")
    if df.empty:
        raise RuntimeError("QLD 데이터를 가져올 수 없습니다. yfinance API를 확인하세요.")
    return df


def calc_sma200(df: pd.DataFrame) -> float:
    """200일 단순이동평균(SMA) 계산."""
    if len(df) < 200:
        return df["Close"].mean()
    return df["Close"].tail(200).mean()


def calc_deviation(close: float, sma200: float) -> float:
    """200일선 괴리율 (%) 계산."""
    return (close - sma200) / sma200 * 100


def calc_weekly_rsi(df: pd.DataFrame, now_kst: datetime) -> tuple[float, str]:
    """
    주봉 RSI(14) 계산.
    - 화~금(KST): 진행형 주봉 RSI (금주 데이터를 임시 포함)
    - 토요일(KST): 확정 주봉 RSI (금요일 마감 반영)
    - 일/월요일(KST): 전주 확정 RSI
    
    Returns: (rsi_value, rsi_type)  rsi_type은 "진행형" 또는 "확정"
    """
    # 일봉 → 주봉 리샘플링
    df_weekly = df["Close"].resample("W-FRI").last().dropna()

    weekday = now_kst.weekday()  # 0=월, 1=화, ..., 5=토, 6=일

    if weekday == 5:  # 토요일 → 확정 주봉
        rsi_type = "확정"
    elif weekday in (1, 2, 3, 4):  # 화~금 → 진행형 주봉
        rsi_type = "진행형"
        # 현재 진행 중인 주의 마지막 종가를 임시 주봉으로 추가
        five_days_ago = df.index[-1] - pd.Timedelta(days=5)
        current_week_data = df["Close"].loc[df.index >= five_days_ago]
        if not current_week_data.empty:
            temp_weekly_close = current_week_data.iloc[-1]
            last_friday = df_weekly.index[-1] if len(df_weekly) > 0 else None
            today_date = df["Close"].index[-1]
            if last_friday is None or today_date > last_friday:
                df_weekly = pd.concat([
                    df_weekly,
                    pd.Series([temp_weekly_close], index=[today_date])
                ])
    else:  # 일/월 → 전주 확정
        rsi_type = "확정"

    # RSI(14) 계산 — Wilder's Smoothing
    delta = df_weekly.diff().dropna()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))

    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    if rsi.empty or pd.isna(rsi.iloc[-1]):
        return 50.0, rsi_type

    return round(float(rsi.iloc[-1]), 1), rsi_type


def determine_status(deviation: float, rsi: float) -> dict:
    """
    괴리율과 RSI를 기반으로 현재 상태를 판정한다.
    
    신호 (3→2→1 순서로 체크, 가장 심한 것부터):
      3단계: RSI ≤ 36 AND 괴리율 ≤ -26%
      2단계: RSI ≤ 42 AND 괴리율 ≤ -18%
      1단계: RSI ≤ 48 AND 괴리율 ≤ -10%
    
    비신호:
      관심: 괴리율 -5%~-10% OR RSI 48~55
      안정: 괴리율 > -5% AND RSI > 55
    """
    for level in STATUS_LEVELS:
        if rsi <= level["rsi_threshold"] and deviation <= level["deviation_threshold"]:
            return level

    if (-10 <= deviation <= -5) or (48 <= rsi <= 55):
        return STATUS_WATCH

    return STATUS_STABLE


def calc_margin_to_stage1(deviation: float, rsi: float) -> dict:
    """1단계 진입까지 남은 마진 계산."""
    dev_target = -10.0
    if deviation <= dev_target:
        dev_margin = 0.0
        dev_progress = 100.0
    elif deviation >= 0:
        dev_margin = abs(deviation - dev_target)
        dev_progress = 0.0
    else:
        dev_margin = abs(deviation - dev_target)
        dev_progress = min(100, max(0, (abs(deviation) / abs(dev_target)) * 100))

    rsi_target = 48.0
    if rsi <= rsi_target:
        rsi_margin = 0.0
        rsi_progress = 100.0
    else:
        rsi_margin = rsi - rsi_target
        rsi_progress = min(100, max(0, ((70 - rsi) / (70 - rsi_target)) * 100))

    return {
        "deviation_margin": round(dev_margin, 2),
        "deviation_progress": round(dev_progress, 1),
        "rsi_margin": round(rsi_margin, 1),
        "rsi_progress": round(rsi_progress, 1),
    }


# ─── 이력 관리 ─────────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    """data/history.json 파일에서 이력을 불러온다."""
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_history(history: list[dict]) -> None:
    """이력을 data/history.json에 저장한다. 최대 HISTORY_MAX_DAYS일치 보관."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = history[-HISTORY_MAX_DAYS:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def update_history(date_str: str, close: float, sma200: float,
                   deviation: float, rsi: float, status_name: str) -> list[dict]:
    """오늘 데이터를 이력에 추가하고 저장한다."""
    history = load_history()

    today_entry = {
        "date": date_str,
        "close": round(close, 2),
        "sma200": round(sma200, 2),
        "deviation": round(deviation, 2),
        "rsi": round(rsi, 1),
        "status": status_name,
    }

    history = [h for h in history if h.get("date") != date_str]
    history.append(today_entry)
    history.sort(key=lambda x: x["date"])

    save_history(history)
    return history


# ─── 이메일 발송 ───────────────────────────────────────────────────────────────

def build_email_html(status: dict, close: float, sma200: float,
                     deviation: float, rsi: float, rsi_type: str,
                     margin: dict, now_kst: datetime) -> str:
    """HTML 형태의 이메일 본문을 생성한다."""
    date_str = now_kst.strftime("%Y년 %m월 %d일 (%a)")

    status_bg_map = {
        "안정": "linear-gradient(135deg, #00d68f22, #00d68f11)",
        "관심": "linear-gradient(135deg, #ffaa0022, #ffaa0011)",
        "1단계": "linear-gradient(135deg, #ff6b3522, #ff6b3511)",
        "2단계": "linear-gradient(135deg, #ff386022, #ff386011)",
        "3단계": "linear-gradient(135deg, #b86bff22, #b86bff11)",
    }
    bg = status_bg_map.get(status["name"], status_bg_map["안정"])

    # 괴리율/RSI 색상 미리 계산
    dev_color = "#ff3860" if deviation < -10 else ("#ffaa00" if deviation < -5 else "#00d68f")
    rsi_color = "#ff3860" if rsi < 42 else ("#ffaa00" if rsi < 48 else "#00d68f")

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0a0e17;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:20px;">

<!-- Header -->
<div style="text-align:center;padding:20px 0;border-bottom:1px solid #1e293b;">
  <h1 style="color:#e2e8f0;font-size:20px;margin:0;">📊 QLD 데일리 모니터링</h1>
  <p style="color:#64748b;font-size:13px;margin:5px 0 0;">{date_str} | {rsi_type} 주봉 RSI</p>
</div>

<!-- 상태 카드 -->
<div style="margin:20px 0;padding:24px;border-radius:16px;background:{bg};border:1px solid {status['color']}44;text-align:center;">
  <div style="font-size:48px;margin-bottom:8px;">{status['emoji']}</div>
  <div style="font-size:28px;font-weight:700;color:{status['color']};margin-bottom:4px;">{status['name']}</div>
  <div style="font-size:14px;color:#94a3b8;">{status['description']}</div>
</div>

<!-- 지표 카드 -->
<table style="width:100%;border-collapse:separate;border-spacing:8px;">
<tr>
  <td style="background:#111827;border-radius:12px;padding:16px;text-align:center;width:33%;border:1px solid #1e293b;">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px;">종가</div>
    <div style="color:#e2e8f0;font-size:24px;font-weight:700;margin-top:4px;">${close:.2f}</div>
  </td>
  <td style="background:#111827;border-radius:12px;padding:16px;text-align:center;width:33%;border:1px solid #1e293b;">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px;">200일선 괴리율</div>
    <div style="color:{dev_color};font-size:24px;font-weight:700;margin-top:4px;">{deviation:+.2f}%</div>
  </td>
  <td style="background:#111827;border-radius:12px;padding:16px;text-align:center;width:33%;border:1px solid #1e293b;">
    <div style="color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px;">주봉 RSI ({rsi_type})</div>
    <div style="color:{rsi_color};font-size:24px;font-weight:700;margin-top:4px;">{rsi:.1f}</div>
  </td>
</tr>
</table>

<!-- 1단계 마진 -->
<div style="margin:20px 0;padding:16px;background:#111827;border-radius:12px;border:1px solid #1e293b;">
  <div style="color:#94a3b8;font-size:13px;font-weight:600;margin-bottom:12px;">📐 1단계 진입까지 남은 거리</div>
  <div style="margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
      <span style="color:#64748b;font-size:12px;">괴리율 → -10%</span>
      <span style="color:#e2e8f0;font-size:12px;">{margin['deviation_margin']:.2f}%p 남음</span>
    </div>
    <div style="background:#1e293b;border-radius:6px;height:8px;overflow:hidden;">
      <div style="background:linear-gradient(90deg,#ff6b35,#ffaa00);height:100%;width:{margin['deviation_progress']}%;border-radius:6px;"></div>
    </div>
  </div>
  <div>
    <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
      <span style="color:#64748b;font-size:12px;">RSI → 48</span>
      <span style="color:#e2e8f0;font-size:12px;">{margin['rsi_margin']:.1f}pt 남음</span>
    </div>
    <div style="background:#1e293b;border-radius:6px;height:8px;overflow:hidden;">
      <div style="background:linear-gradient(90deg,#ff6b35,#ffaa00);height:100%;width:{margin['rsi_progress']}%;border-radius:6px;"></div>
    </div>
  </div>
</div>

<!-- 매수 집행 규칙 -->
<div style="margin:20px 0;padding:16px;background:#111827;border-radius:12px;border:1px solid #1e293b;">
  <div style="color:#94a3b8;font-size:13px;font-weight:600;margin-bottom:8px;">📋 매수 집행 규칙 (40:40:20 DCA)</div>
  <div style="color:#64748b;font-size:12px;line-height:1.6;">
    • <b style="color:#e2e8f0;">1차 매수</b>: 1단계 신호 시 현금 40%를 8주간 매주 1/8 매수<br>
    • <b style="color:#e2e8f0;">2차 매수</b>: 8주 후 신호 유지 시 남은 현금 40%를 8주 추가 매수<br>
    • <b style="color:#e2e8f0;">3차 매수</b>: 잔여 20% + 배당금으로 소진 시까지 매수<br>
    • <b style="color:#e2e8f0;">자금 순서</b>: CMA → 단기국채(0046A0) 매도 → 418660 매수
  </div>
</div>

<!-- Footer -->
<div style="text-align:center;padding:16px 0;border-top:1px solid #1e293b;margin-top:20px;">
  <p style="color:#475569;font-size:11px;margin:0;">이 이메일은 자동 생성되었습니다. 투자 판단의 책임은 본인에게 있습니다.</p>
</div>

</div>
</body>
</html>"""


def send_email(subject: str, html_body: str, dry_run: bool = False) -> None:
    """Gmail SMTP로 이메일을 발송한다."""
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_address or not gmail_password:
        print("⚠️  GMAIL_ADDRESS 또는 GMAIL_APP_PASSWORD 환경변수가 설정되지 않았습니다.")
        if dry_run:
            print("   (--dry-run 모드이므로 계속 진행합니다)")
            return
        print("   이메일 발송을 건너뜁니다.")
        return

    if dry_run:
        print(f"\n📧 [DRY-RUN] 이메일 발송 시뮬레이션")
        print(f"   To: {gmail_address}")
        print(f"   Subject: {subject}")
        print(f"   Body length: {len(html_body)} chars")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address  # 자기 자신에게 발송

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, gmail_address, msg.as_string())
        print(f"✅ 이메일 발송 완료: {subject}")
    except Exception as e:
        print(f"❌ 이메일 발송 실패: {e}")


# ─── HTML 대시보드 생성 ────────────────────────────────────────────────────────

def generate_dashboard_html(status: dict, close: float, sma200: float,
                            deviation: float, rsi: float, rsi_type: str,
                            margin: dict, history: list[dict],
                            last_updated: str, current_year: int) -> str:
    """index.html 대시보드를 생성한다."""
    # 차트 데이터 (최근 30일)
    chart_json = json.dumps(history[-30:], ensure_ascii=False)

    # 괴리율/RSI 색상 미리 계산
    dev_color = "#ff3860" if deviation < -10 else ("#ffaa00" if deviation < -5 else "#00d68f")
    rsi_color = "#ff3860" if rsi < 42 else ("#ffaa00" if rsi < 48 else "#00d68f")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QLD 모니터링 대시보드</title>
    <meta name="description" content="QLD(나스닥100 2배 레버리지 ETF) 일일 모니터링 대시보드 — 200일선 괴리율, 주봉 RSI, 매수 신호 상태">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0a0e17;
            --bg-surface: rgba(255,255,255,0.03);
            --border-color: rgba(255,255,255,0.08);
            --text-primary: #f1f5f9;
            --text-secondary: #8b9bb4;
            --status-color: {status['color']};
        }}

        * {{ margin:0; padding:0; box-sizing:border-box; font-family:'Inter',system-ui,-apple-system,sans-serif; }}

        body {{
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            align-items: center;
        }}

        .container {{ width:100%; max-width:1200px; display:flex; flex-direction:column; gap:1.5rem; }}

        /* ── Header ── */
        header {{
            display:flex; justify-content:space-between; align-items:center;
            padding-bottom:1rem; border-bottom:1px solid var(--border-color);
            flex-wrap: wrap; gap: 0.5rem;
        }}
        header h1 {{ font-size:1.5rem; font-weight:700; letter-spacing:-0.5px; }}
        .last-updated {{ color:var(--text-secondary); font-size:0.85rem; }}

        /* ── Glass Card ── */
        .glass-card {{
            background: var(--bg-surface);
            backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 1.5rem;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }}
        .glass-card:hover {{ transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0,0,0,0.3); }}

        /* ── Hero Status Card ── */
        .hero-card {{
            display:flex; flex-direction:column; align-items:center; text-align:center;
            padding: 3rem 1.5rem; position:relative; overflow:hidden;
            border-top: 4px solid var(--status-color);
            box-shadow: 0 8px 32px rgba(0,0,0,0.2), 0 0 40px color-mix(in srgb, var(--status-color) 20%, transparent);
            animation: pulse-glow 3s infinite alternate;
        }}
        @keyframes pulse-glow {{
            0% {{ box-shadow: 0 8px 32px rgba(0,0,0,0.2), 0 0 20px color-mix(in srgb, var(--status-color) 13%, transparent); }}
            100% {{ box-shadow: 0 8px 32px rgba(0,0,0,0.2), 0 0 50px color-mix(in srgb, var(--status-color) 33%, transparent); }}
        }}
        .hero-emoji {{ font-size:4rem; margin-bottom:1rem; }}
        .hero-title {{ font-size:2.5rem; font-weight:800; color:var(--status-color); margin-bottom:0.5rem; letter-spacing:-1px; }}
        .hero-desc {{ font-size:1.1rem; color:var(--text-secondary); max-width:600px; line-height:1.5; }}

        /* ── Metrics Grid ── */
        .metrics-grid {{ display:grid; grid-template-columns:repeat(3, 1fr); gap:1.5rem; }}
        .metric-title {{
            color:var(--text-secondary); font-size:0.8rem; font-weight:500;
            text-transform:uppercase; letter-spacing:1.5px; margin-bottom:0.5rem;
        }}
        .metric-value {{ font-size:2rem; font-weight:700; margin-bottom:0.25rem; }}
        .metric-sub {{ font-size:0.85rem; color:var(--text-secondary); }}

        /* ── Progress Bars ── */
        .progress-section {{ display:grid; grid-template-columns:repeat(2, 1fr); gap:1.5rem; }}
        .progress-container {{ margin-top:0.5rem; }}
        .progress-label {{ display:flex; justify-content:space-between; margin-bottom:0.5rem; font-size:0.85rem; }}
        .progress-label span:first-child {{ color:var(--text-secondary); }}
        .progress-bar-bg {{
            height:10px; background:rgba(255,255,255,0.08); border-radius:5px;
            overflow:hidden; position:relative;
        }}
        .progress-fill {{
            height:100%; border-radius:5px; width:0%;
            background:linear-gradient(90deg, #ff6b35, #ffaa00);
            transition: width 1.5s cubic-bezier(0.22,1,0.36,1);
        }}
        .progress-fill.reached {{
            background:linear-gradient(90deg, #ff3860, #ff6b35);
        }}

        /* ── Chart ── */
        .chart-container {{ position:relative; height:350px; width:100%; padding:0.5rem 0; }}
        .chart-container canvas {{ width:100% !important; height:100% !important; }}
        .chart-legend {{
            display:flex; gap:1.5rem; margin-top:0.75rem; justify-content:center; flex-wrap:wrap;
        }}
        .chart-legend span {{ font-size:0.8rem; display:flex; align-items:center; gap:0.4rem; }}
        .legend-dot {{ width:10px; height:3px; border-radius:2px; display:inline-block; }}

        /* ── Accordion ── */
        .accordion-item {{
            border:1px solid var(--border-color); border-radius:12px; overflow:hidden;
            background:rgba(255,255,255,0.02); margin-bottom:1rem;
        }}
        .accordion-header {{
            padding:1.25rem 1.5rem; display:flex; justify-content:space-between; align-items:center;
            cursor:pointer; font-weight:600; background:rgba(255,255,255,0.01);
            transition:background 0.2s ease; user-select:none;
        }}
        .accordion-header:hover {{ background:rgba(255,255,255,0.05); }}
        .accordion-icon {{ transition:transform 0.3s ease; }}
        .accordion-content {{
            max-height:0; overflow:hidden; transition:max-height 0.4s ease-out;
            padding:0 1.5rem; color:var(--text-secondary); line-height:1.7; font-size:0.9rem;
        }}
        .accordion-item.active .accordion-content {{
            padding:1.25rem 1.5rem; max-height:2000px; border-top:1px solid var(--border-color);
        }}
        .accordion-item.active .accordion-icon {{ transform:rotate(180deg); }}

        .guide-table {{ width:100%; border-collapse:collapse; margin:1rem 0; }}
        .guide-table th, .guide-table td {{
            text-align:left; padding:0.75rem; border-bottom:1px solid var(--border-color); font-size:0.85rem;
        }}
        .guide-table th {{ color:var(--text-primary); font-weight:600; }}
        .guide-table td {{ color:var(--text-secondary); }}
        .guide-table tr:last-child td {{ border-bottom:none; }}

        /* ── Footer ── */
        footer {{
            text-align:center; padding:2rem 0; color:var(--text-secondary);
            font-size:0.75rem; border-top:1px solid var(--border-color); margin-top:1rem;
            line-height:1.6;
        }}

        /* ── Portfolio Section ── */
        .pf-section {{ margin-top:0.5rem; }}
        .pf-section .metric-title {{ margin-bottom:1rem; }}
        .pf-add-form {{
            display:grid; grid-template-columns:2fr 1fr 1fr auto; gap:0.5rem;
            margin-bottom:1rem; align-items:end;
        }}
        .pf-add-form label {{ font-size:0.75rem; color:var(--text-secondary); display:block; margin-bottom:0.25rem; }}
        .pf-input {{
            width:100%; padding:0.6rem 0.75rem; border-radius:8px;
            border:1px solid var(--border-color); background:rgba(255,255,255,0.05);
            color:var(--text-primary); font-size:0.9rem; font-family:inherit;
            outline:none; transition:border 0.2s;
        }}
        .pf-input:focus {{ border-color:var(--status-color); }}
        .pf-input::placeholder {{ color:rgba(255,255,255,0.2); }}
        .pf-btn {{
            padding:0.6rem 1.2rem; border-radius:8px; border:none;
            background:linear-gradient(135deg, #3b82f6, #2563eb); color:#fff;
            font-weight:600; font-size:0.85rem; cursor:pointer; transition:all 0.2s;
            white-space:nowrap;
        }}
        .pf-btn:hover {{ transform:translateY(-1px); box-shadow:0 4px 12px rgba(59,130,246,0.4); }}
        .pf-btn-sm {{
            padding:0.3rem 0.6rem; border-radius:6px; border:none;
            background:rgba(255,56,96,0.15); color:#ff3860;
            font-size:0.75rem; cursor:pointer; transition:all 0.2s;
        }}
        .pf-btn-sm:hover {{ background:rgba(255,56,96,0.3); }}

        .pf-table {{ width:100%; border-collapse:collapse; }}
        .pf-table th {{
            text-align:left; padding:0.6rem; font-size:0.75rem; color:var(--text-secondary);
            text-transform:uppercase; letter-spacing:1px; border-bottom:1px solid var(--border-color);
        }}
        .pf-table td {{
            padding:0.6rem; font-size:0.85rem; border-bottom:1px solid rgba(255,255,255,0.04);
            color:var(--text-primary);
        }}
        .pf-table tr:last-child td {{ border-bottom:none; }}
        .pf-table .pf-total-row td {{
            font-weight:700; border-top:1px solid var(--border-color);
            padding-top:0.8rem; font-size:0.95rem;
        }}
        .pf-empty {{ text-align:center; padding:2rem; color:var(--text-secondary); font-size:0.9rem; }}

        .pf-visual-grid {{ display:grid; grid-template-columns:280px 1fr; gap:1.5rem; margin-top:1.5rem; align-items:start; }}
        .pf-donut-wrap {{ display:flex; flex-direction:column; align-items:center; }}
        .pf-donut-wrap canvas {{ width:240px !important; height:240px !important; }}

        .pf-compare-bars {{ display:flex; flex-direction:column; gap:0.75rem; }}
        .pf-compare-item {{ }}
        .pf-compare-label {{
            display:flex; justify-content:space-between; font-size:0.8rem;
            margin-bottom:0.3rem; color:var(--text-secondary);
        }}
        .pf-compare-label strong {{ color:var(--text-primary); }}
        .pf-bar-track {{
            height:20px; background:rgba(255,255,255,0.06); border-radius:6px;
            position:relative; overflow:hidden;
        }}
        .pf-bar-current {{
            height:100%; border-radius:6px; position:absolute; top:0; left:0;
            transition:width 0.8s ease; opacity:0.9;
        }}
        .pf-bar-target {{
            height:100%; border-top:2px dashed rgba(255,255,255,0.35);
            border-right:2px dashed rgba(255,255,255,0.35);
            position:absolute; top:0; left:0; border-radius:6px;
            transition:width 0.8s ease;
        }}
        .pf-compare-legend {{
            display:flex; gap:1.5rem; justify-content:center; margin-top:0.5rem;
            font-size:0.75rem; color:var(--text-secondary);
        }}
        .pf-legend-box {{ width:12px; height:12px; border-radius:3px; display:inline-block; vertical-align:middle; margin-right:4px; }}

        /* ── Responsive ── */
        @media (max-width: 768px) {{
            body {{ padding:1rem; }}
            .metrics-grid {{ grid-template-columns:1fr; }}
            .progress-section {{ grid-template-columns:1fr; }}
            .hero-title {{ font-size:2rem; }}
            .hero-emoji {{ font-size:3rem; }}
            .hero-card {{ padding:2rem 1rem; }}
            .metric-value {{ font-size:1.6rem; }}
            header {{ flex-direction:column; align-items:flex-start; }}
            .pf-add-form {{ grid-template-columns:1fr 1fr; }}
            .pf-visual-grid {{ grid-template-columns:1fr; }}
            .pf-donut-wrap canvas {{ width:200px !important; height:200px !important; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>📊 QLD 모니터링 대시보드</h1>
            <div class="last-updated">마지막 업데이트: {last_updated}</div>
        </header>

        <!-- 상태 히어로 카드 -->
        <div class="glass-card hero-card" id="hero-status">
            <div class="hero-emoji">{status['emoji']}</div>
            <div class="hero-title">{status['name']}</div>
            <div class="hero-desc">{status['description']}</div>
        </div>

        <!-- 핵심 지표 카드 -->
        <div class="metrics-grid">
            <div class="glass-card" id="metric-close">
                <div class="metric-title">현재가 (QLD)</div>
                <div class="metric-value">${close:.2f}</div>
                <div class="metric-sub">SMA200: ${sma200:.2f}</div>
            </div>
            <div class="glass-card" id="metric-deviation">
                <div class="metric-title">200일선 괴리율</div>
                <div class="metric-value" style="color:{dev_color}">{deviation:+.2f}%</div>
                <div class="metric-sub">임계: -10% / -18% / -26%</div>
            </div>
            <div class="glass-card" id="metric-rsi">
                <div class="metric-title">주봉 RSI ({rsi_type})</div>
                <div class="metric-value" style="color:{rsi_color}">{rsi:.1f}</div>
                <div class="metric-sub">임계: 48 / 42 / 36</div>
            </div>
        </div>

        <!-- 1단계 마진 프로그레스바 -->
        <div class="progress-section">
            <div class="glass-card" id="progress-deviation">
                <div class="metric-title">1단계 진입까지 — 괴리율</div>
                <div class="progress-container">
                    <div class="progress-label">
                        <span>현재: {deviation:+.2f}%</span>
                        <span>목표: -10% ({margin['deviation_margin']:.2f}%p 남음)</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-fill{' reached' if margin['deviation_progress'] >= 100 else ''}" id="dev-bar"></div>
                    </div>
                </div>
            </div>
            <div class="glass-card" id="progress-rsi">
                <div class="metric-title">1단계 진입까지 — RSI</div>
                <div class="progress-container">
                    <div class="progress-label">
                        <span>현재: {rsi:.1f}</span>
                        <span>목표: 48 ({margin['rsi_margin']:.1f}pt 남음)</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-fill{' reached' if margin['rsi_progress'] >= 100 else ''}" id="rsi-bar"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 추세 차트 -->
        <div class="glass-card" id="chart-section">
            <div class="metric-title" style="margin-bottom:0.5rem;">30일 트렌드</div>
            <div class="chart-container">
                <canvas id="trendChart"></canvas>
            </div>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:#ffaa00;"></span> 괴리율 (좌축)</span>
                <span><span class="legend-dot" style="background:#00d68f;"></span> RSI (우축)</span>
                <span><span class="legend-dot" style="background:rgba(255,56,96,0.5);"></span> 임계선</span>
            </div>
        </div>

        <!-- 내 포트폴리오 현황 -->
        <div class="glass-card pf-section" id="portfolio-section">
            <div class="metric-title">💼 내 포트폴리오 현황</div>

            <!-- 종목 추가 폼 -->
            <div class="pf-add-form">
                <div>
                    <label>종목명</label>
                    <input type="text" class="pf-input" id="pf-name" placeholder="예: 삼성전자">
                </div>
                <div>
                    <label>수량</label>
                    <input type="number" class="pf-input" id="pf-qty" placeholder="0" min="0" step="any">
                </div>
                <div>
                    <label>단가 (₩ 또는 $)</label>
                    <input type="number" class="pf-input" id="pf-price" placeholder="0" min="0" step="any">
                </div>
                <div style="padding-top:1.1rem;">
                    <button class="pf-btn" onclick="pfAdd()">+ 추가</button>
                </div>
            </div>

            <!-- 보유 종목 테이블 -->
            <div id="pf-table-wrap"></div>

            <!-- 시각화: 도넛 + 비교 바 -->
            <div class="pf-visual-grid" id="pf-visuals" style="display:none;">
                <div class="pf-donut-wrap">
                    <canvas id="pfDonut" width="240" height="240"></canvas>
                </div>
                <div>
                    <div style="font-size:0.8rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:1px;margin-bottom:0.75rem;">현재 vs 목표 배분</div>
                    <div id="pf-compare"></div>
                    <div class="pf-compare-legend">
                        <span><span class="pf-legend-box" style="background:#3b82f6;opacity:0.9;"></span> 현재</span>
                        <span><span class="pf-legend-box" style="border:2px dashed rgba(255,255,255,0.35);background:transparent;"></span> 목표</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- 포트폴리오 가이드 아코디언 -->
        <div id="guide-section">
            <div class="accordion-item">
                <div class="accordion-header" onclick="toggleAccordion(this)">
                    <span>📋 자산 배분 구조</span>
                    <svg class="accordion-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </div>
                <div class="accordion-content">
                    <table class="guide-table">
                        <thead><tr><th>구분</th><th>자산</th><th>비중</th></tr></thead>
                        <tbody>
                            <tr><td>🔵 주력</td><td>418660 TIGER 미국나스닥100레버리지(합성)</td><td>60%</td></tr>
                            <tr><td>🟢 실탄</td><td>486290 TIGER 미국나스닥100타겟데일리커버드콜</td><td>30%</td></tr>
                            <tr><td>🟡 직투</td><td>DGRO iShares Core Dividend Growth ETF</td><td>10%</td></tr>
                            <tr><td colspan="3" style="padding-top:1rem;font-weight:600;color:var(--text-primary);">현금성 자산 (30%)</td></tr>
                            <tr><td>💰 국채</td><td>0046A0 TIGER 미국초단기국채</td><td>80%</td></tr>
                            <tr><td>💵 현금</td><td>CMA 통장</td><td>20%</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="accordion-item">
                <div class="accordion-header" onclick="toggleAccordion(this)">
                    <span>📐 분할 매수 집행 규칙 (40:40:20 DCA)</span>
                    <svg class="accordion-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </div>
                <div class="accordion-content">
                    <table class="guide-table">
                        <thead><tr><th>단계</th><th>규모</th><th>실행 방법</th></tr></thead>
                        <tbody>
                            <tr><td>1차 매수</td><td>현금의 40%</td><td>1단계 신호 진입 시, 8주간 매주 1/8씩 기계적 매수</td></tr>
                            <tr><td>2차 매수</td><td>남은 현금의 40%</td><td>8주 경과 후 신호 유지 시, 2차 8주(총 16주) 매수</td></tr>
                            <tr><td>3차 매수</td><td>잔여 20% + 배당금</td><td>커버드콜/DGRO 배당금을 매월 신규 실탄으로 소진까지 매수</td></tr>
                        </tbody>
                    </table>
                    <p style="margin-top:1rem;font-size:0.85rem;"><strong>자금 인출 순서:</strong> CMA 잔액 우선 → 부족 시 0046A0(단기국채) 매도 → 418660 매수</p>
                </div>
            </div>

            <div class="accordion-item">
                <div class="accordion-header" onclick="toggleAccordion(this)">
                    <span>🚦 상태 매트릭스 가이드</span>
                    <svg class="accordion-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </div>
                <div class="accordion-content">
                    <table class="guide-table">
                        <thead><tr><th>상태</th><th>조건</th><th>설명</th></tr></thead>
                        <tbody>
                            <tr><td>🟢 안정</td><td>괴리율 &gt; -5% AND RSI &gt; 55</td><td>평시 관망 구간</td></tr>
                            <tr><td>🟡 관심</td><td>괴리율 -5%~-10% OR RSI 48~55</td><td>매수 임계치 근접, 현금 점검</td></tr>
                            <tr><td>🟠 1단계</td><td>RSI ≤ 48 AND 괴리율 ≤ -10%</td><td>분할 매수 시작 (8주 1차)</td></tr>
                            <tr><td>🔴 2단계</td><td>RSI ≤ 42 AND 괴리율 ≤ -18%</td><td>매력적 매수 (계획 이행)</td></tr>
                            <tr><td>🟣 3단계</td><td>RSI ≤ 36 AND 괴리율 ≤ -26%</td><td>역사적 바닥 (전력 매수)</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <footer>
            ⚠️ 이 대시보드는 자동 생성된 참고 자료이며, 투자 조언이 아닙니다.<br>
            모든 투자 판단의 책임은 본인에게 있습니다.<br>
            &copy; {current_year} QLD Monitoring Dashboard
        </footer>
    </div>

    <script>
        // ── Accordion ──
        function toggleAccordion(el) {{
            el.parentElement.classList.toggle('active');
        }}

        // ── Progress Bar Animation ──
        window.addEventListener('load', function() {{
            setTimeout(function() {{
                document.getElementById('dev-bar').style.width = '{margin["deviation_progress"]:.1f}%';
                document.getElementById('rsi-bar').style.width = '{margin["rsi_progress"]:.1f}%';
            }}, 300);
        }});

        // ── Chart ──
        const chartData = {chart_json};

        function drawChart() {{
            const canvas = document.getElementById('trendChart');
            if (!canvas || !chartData || chartData.length < 2) return;

            const ctx = canvas.getContext('2d');
            const dpr = window.devicePixelRatio || 1;
            const rect = canvas.parentElement.getBoundingClientRect();
            canvas.width = rect.width * dpr;
            canvas.height = rect.height * dpr;
            canvas.style.width = rect.width + 'px';
            canvas.style.height = rect.height + 'px';
            ctx.scale(dpr, dpr);

            const W = rect.width;
            const H = rect.height;
            const pad = {{ top:25, right:55, bottom:35, left:55 }};
            const pW = W - pad.left - pad.right;
            const pH = H - pad.top - pad.bottom;
            const n = chartData.length;

            const devs = chartData.map(d => d.deviation);
            const rsis = chartData.map(d => d.rsi);
            const dates = chartData.map(d => d.date);

            // Y축 범위 (여유 10%)
            const dMin = Math.min(...devs, -30) - 3;
            const dMax = Math.max(...devs, 5) + 3;
            const rMin = Math.min(...rsis, 30) - 3;
            const rMax = Math.max(...rsis, 80) + 3;

            const xOf = i => pad.left + (i / (n - 1)) * pW;
            const yDev = v => pad.top + pH - ((v - dMin) / (dMax - dMin)) * pH;
            const yRsi = v => pad.top + pH - ((v - rMin) / (rMax - rMin)) * pH;

            // 배경 그리드
            ctx.strokeStyle = 'rgba(255,255,255,0.05)';
            ctx.lineWidth = 1;
            for (let i = 0; i < 5; i++) {{
                const y = pad.top + (pH / 4) * i;
                ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
            }}

            // X축 날짜 라벨
            ctx.fillStyle = '#64748b';
            ctx.font = '10px Inter';
            ctx.textAlign = 'center';
            const step = Math.max(1, Math.floor(n / 6));
            for (let i = 0; i < n; i += step) {{
                ctx.fillText(dates[i].substring(5), xOf(i), H - 8);
            }}

            // 괴리율 임계선 (좌축)
            const devThresh = [-10, -18, -26];
            const devColors = ['rgba(255,107,53,0.3)', 'rgba(255,56,96,0.3)', 'rgba(184,107,255,0.3)'];
            ctx.textAlign = 'right';
            devThresh.forEach((t, idx) => {{
                const y = yDev(t);
                if (y >= pad.top && y <= H - pad.bottom) {{
                    ctx.beginPath(); ctx.setLineDash([4,4]);
                    ctx.strokeStyle = devColors[idx]; ctx.lineWidth = 1;
                    ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y);
                    ctx.stroke(); ctx.setLineDash([]);
                    ctx.fillStyle = devColors[idx].replace('0.3','0.8');
                    ctx.fillText(t + '%', pad.left - 5, y + 3);
                }}
            }});

            // RSI 임계선 (우축)
            const rsiThresh = [48, 42, 36];
            const rsiColors = ['rgba(255,107,53,0.3)', 'rgba(255,56,96,0.3)', 'rgba(184,107,255,0.3)'];
            ctx.textAlign = 'left';
            rsiThresh.forEach((t, idx) => {{
                const y = yRsi(t);
                if (y >= pad.top && y <= H - pad.bottom) {{
                    ctx.beginPath(); ctx.setLineDash([4,4]);
                    ctx.strokeStyle = rsiColors[idx]; ctx.lineWidth = 1;
                    ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y);
                    ctx.stroke(); ctx.setLineDash([]);
                    ctx.fillStyle = rsiColors[idx].replace('0.3','0.8');
                    ctx.fillText(t, W - pad.right + 5, y + 3);
                }}
            }});

            // 괴리율 라인 (금색)
            ctx.beginPath();
            ctx.moveTo(xOf(0), yDev(devs[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yDev(devs[i]));
            ctx.strokeStyle = '#ffaa00'; ctx.lineWidth = 2.5; ctx.setLineDash([]); ctx.stroke();

            // 괴리율 영역 fill
            ctx.beginPath();
            ctx.moveTo(xOf(0), yDev(devs[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yDev(devs[i]));
            ctx.lineTo(xOf(n-1), H - pad.bottom); ctx.lineTo(xOf(0), H - pad.bottom); ctx.closePath();
            ctx.fillStyle = 'rgba(255,170,0,0.08)'; ctx.fill();

            // RSI 라인 (초록)
            ctx.beginPath();
            ctx.moveTo(xOf(0), yRsi(rsis[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yRsi(rsis[i]));
            ctx.strokeStyle = '#00d68f'; ctx.lineWidth = 2.5; ctx.stroke();

            // RSI 영역 fill
            ctx.beginPath();
            ctx.moveTo(xOf(0), yRsi(rsis[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yRsi(rsis[i]));
            ctx.lineTo(xOf(n-1), H - pad.bottom); ctx.lineTo(xOf(0), H - pad.bottom); ctx.closePath();
            ctx.fillStyle = 'rgba(0,214,143,0.08)'; ctx.fill();

            // 최신 데이터 포인트 dot
            ctx.beginPath(); ctx.arc(xOf(n-1), yDev(devs[n-1]), 4, 0, Math.PI*2);
            ctx.fillStyle = '#ffaa00'; ctx.fill();
            ctx.beginPath(); ctx.arc(xOf(n-1), yRsi(rsis[n-1]), 4, 0, Math.PI*2);
            ctx.fillStyle = '#00d68f'; ctx.fill();

            // Y축 라벨 — 좌: 괴리율
            ctx.textAlign = 'right'; ctx.fillStyle = '#ffaa00'; ctx.font = '10px Inter';
            for (let i = 0; i <= 4; i++) {{
                const v = dMin + (dMax - dMin) * (i / 4);
                ctx.fillText(v.toFixed(0) + '%', pad.left - 8, pad.top + pH - (pH / 4) * i + 3);
            }}
            // Y축 라벨 — 우: RSI
            ctx.textAlign = 'left'; ctx.fillStyle = '#00d68f';
            for (let i = 0; i <= 4; i++) {{
                const v = rMin + (rMax - rMin) * (i / 4);
                ctx.fillText(v.toFixed(0), W - pad.right + 8, pad.top + pH - (pH / 4) * i + 3);
            }}
        }}

        drawChart();
        window.addEventListener('resize', drawChart);

        // ══════════════════════════════════════════════
        // ── Portfolio Manager (localStorage) ──
        // ══════════════════════════════════════════════
        const PF_KEY = 'qld_portfolio_v1';
        const PF_COLORS = [
            '#3b82f6','#00d68f','#ffaa00','#ff6b35','#b86bff',
            '#ff3860','#06b6d4','#f43f5e','#84cc16','#a78bfa',
            '#fb923c','#22d3ee','#e879f9','#facc15','#4ade80'
        ];
        const PF_TARGET = [
            {{ name:'418660 TIGER 레버리지', pct:42, color:'#3b82f6' }},
            {{ name:'486290 TIGER 커버드콜', pct:21, color:'#00d68f' }},
            {{ name:'DGRO 배당성장', pct:7, color:'#ffaa00' }},
            {{ name:'0046A0 초단기국채', pct:24, color:'#ff6b35' }},
            {{ name:'CMA 현금', pct:6, color:'#b86bff' }},
        ];

        function pfLoad() {{
            try {{ return JSON.parse(localStorage.getItem(PF_KEY)) || []; }}
            catch {{ return []; }}
        }}
        function pfSave(h) {{ localStorage.setItem(PF_KEY, JSON.stringify(h)); }}

        function pfAdd() {{
            const name = document.getElementById('pf-name').value.trim();
            const qty = parseFloat(document.getElementById('pf-qty').value);
            const price = parseFloat(document.getElementById('pf-price').value);
            if (!name || isNaN(qty) || isNaN(price) || qty <= 0 || price <= 0) {{
                alert('종목명, 수량, 단가를 모두 입력해주세요.');
                return;
            }}
            const h = pfLoad();
            h.push({{ id: Date.now(), name, qty, price }});
            pfSave(h);
            document.getElementById('pf-name').value = '';
            document.getElementById('pf-qty').value = '';
            document.getElementById('pf-price').value = '';
            pfRender();
        }}

        function pfRemove(id) {{
            pfSave(pfLoad().filter(x => x.id !== id));
            pfRender();
        }}

        function pfEdit(id) {{
            const h = pfLoad();
            const item = h.find(x => x.id === id);
            if (!item) return;
            const newQty = prompt('수량 수정:', item.qty);
            if (newQty === null) return;
            const newPrice = prompt('단가 수정:', item.price);
            if (newPrice === null) return;
            item.qty = parseFloat(newQty) || item.qty;
            item.price = parseFloat(newPrice) || item.price;
            pfSave(h);
            pfRender();
        }}

        function pfFmt(n) {{
            if (n >= 1e8) return (n/1e8).toFixed(1) + '억';
            if (n >= 1e4) return (n/1e4).toFixed(0) + '만';
            return n.toLocaleString();
        }}

        function pfRender() {{
            const holdings = pfLoad();
            const wrap = document.getElementById('pf-table-wrap');
            const visuals = document.getElementById('pf-visuals');

            if (holdings.length === 0) {{
                wrap.innerHTML = '<div class="pf-empty">종목을 추가하면 자산 배분 현황이 여기에 표시됩니다.</div>';
                visuals.style.display = 'none';
                return;
            }}

            // 평가액 계산
            let total = 0;
            holdings.forEach(h => {{ h.value = h.qty * h.price; total += h.value; }});

            // 테이블 렌더링
            let rows = holdings.map((h, i) => {{
                const pct = total > 0 ? (h.value / total * 100).toFixed(1) : '0.0';
                const c = PF_COLORS[i % PF_COLORS.length];
                return `<tr>
                    <td><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${{c}};margin-right:6px;"></span>${{h.name}}</td>
                    <td style="text-align:right;">${{h.qty.toLocaleString()}}</td>
                    <td style="text-align:right;">₩${{h.price.toLocaleString()}}</td>
                    <td style="text-align:right;">₩${{pfFmt(h.value)}}</td>
                    <td style="text-align:right;color:var(--text-secondary);">${{pct}}%</td>
                    <td style="text-align:right;white-space:nowrap;">
                        <button class="pf-btn-sm" onclick="pfEdit(${{h.id}})" style="background:rgba(59,130,246,0.15);color:#3b82f6;margin-right:4px;">수정</button>
                        <button class="pf-btn-sm" onclick="pfRemove(${{h.id}})">삭제</button>
                    </td>
                </tr>`;
            }}).join('');

            wrap.innerHTML = `<table class="pf-table">
                <thead><tr><th>종목</th><th style="text-align:right;">수량</th><th style="text-align:right;">단가</th><th style="text-align:right;">평가액</th><th style="text-align:right;">비중</th><th></th></tr></thead>
                <tbody>${{rows}}
                <tr class="pf-total-row"><td colspan="3">합계</td><td style="text-align:right;">₩${{pfFmt(total)}}</td><td colspan="2"></td></tr>
                </tbody></table>`;

            // 시각화
            visuals.style.display = 'grid';
            pfDrawDonut(holdings, total);
            pfDrawCompare(holdings, total);
        }}

        function pfDrawDonut(holdings, total) {{
            const canvas = document.getElementById('pfDonut');
            const dpr = window.devicePixelRatio || 1;
            const size = 240;
            canvas.width = size * dpr; canvas.height = size * dpr;
            canvas.style.width = size + 'px'; canvas.style.height = size + 'px';
            const ctx = canvas.getContext('2d');
            ctx.scale(dpr, dpr);

            const cx = size/2, cy = size/2, R = 90, r = 55;
            let angle = -Math.PI / 2;

            holdings.forEach((h, i) => {{
                const pct = total > 0 ? h.value / total : 0;
                const sweep = pct * Math.PI * 2;
                ctx.beginPath();
                ctx.arc(cx, cy, R, angle, angle + sweep);
                ctx.arc(cx, cy, r, angle + sweep, angle, true);
                ctx.closePath();
                ctx.fillStyle = PF_COLORS[i % PF_COLORS.length];
                ctx.fill();
                angle += sweep;
            }});

            // 중앙 텍스트
            ctx.fillStyle = '#f1f5f9'; ctx.font = '700 16px Inter'; ctx.textAlign = 'center';
            ctx.fillText('₩' + pfFmt(total), cx, cy - 2);
            ctx.fillStyle = '#8b9bb4'; ctx.font = '11px Inter';
            ctx.fillText('총 자산', cx, cy + 16);
        }}

        function pfDrawCompare(holdings, total) {{
            const container = document.getElementById('pf-compare');
            // 보유 종목을 목표 카테고리별로 매핑
            const currentMap = {{}};
            holdings.forEach(h => {{
                const key = h.name.trim();
                currentMap[key] = (currentMap[key] || 0) + (total > 0 ? h.value / total * 100 : 0);
            }});

            // 목표 vs 현재 비교 바 생성
            let html = '';
            const maxPct = 100;

            // 목표 포트폴리오 항목 표시
            PF_TARGET.forEach(t => {{
                // 보유 종목 중 목표 카테고리와 매칭되는 것 찾기
                let currentPct = 0;
                Object.keys(currentMap).forEach(k => {{
                    if (k.includes(t.name.split(' ')[0])) {{
                        currentPct += currentMap[k];
                        delete currentMap[k]; // 매칭된 것 제거
                    }}
                }});

                const diff = currentPct - t.pct;
                const diffStr = diff > 0 ? `+${{diff.toFixed(1)}}%` : `${{diff.toFixed(1)}}%`;
                const diffColor = Math.abs(diff) < 3 ? '#00d68f' : (diff < 0 ? '#ff3860' : '#ffaa00');

                html += `<div class="pf-compare-item">
                    <div class="pf-compare-label"><strong>${{t.name}}</strong><span style="color:${{diffColor}}">${{currentPct.toFixed(1)}}% / ${{t.pct}}% (${{diffStr}})</span></div>
                    <div class="pf-bar-track">
                        <div class="pf-bar-current" style="width:${{Math.min(currentPct, maxPct)}}%;background:${{t.color}};"></div>
                        <div class="pf-bar-target" style="width:${{t.pct}}%;"></div>
                    </div>
                </div>`;
            }});

            // 매칭 안 된 기타 종목들
            let otherPct = 0;
            Object.values(currentMap).forEach(v => otherPct += v);
            if (otherPct > 0.5) {{
                html += `<div class="pf-compare-item">
                    <div class="pf-compare-label"><strong>기타 (정리 대상)</strong><span style="color:#ff3860">${{otherPct.toFixed(1)}}% / 0% (+${{otherPct.toFixed(1)}}%)</span></div>
                    <div class="pf-bar-track">
                        <div class="pf-bar-current" style="width:${{Math.min(otherPct, maxPct)}}%;background:#64748b;"></div>
                    </div>
                </div>`;
            }}

            container.innerHTML = html;
        }}

        // 초기 렌더링
        pfRender();
    </script>
</body>
</html>"""


# ─── 메인 실행 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="QLD 자동 매수 알림 시스템")
    parser.add_argument("--dry-run", action="store_true",
                        help="이메일 발송 없이 테스트 실행")
    args = parser.parse_args()

    now_kst = datetime.now(KST)
    weekday = now_kst.weekday()

    print(f"🕐 실행 시간: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"   요일: {['월','화','수','목','금','토','일'][weekday]}요일")

    # ── 1. 데이터 수집 ──
    print("\n📡 QLD 데이터 수집 중...")
    try:
        df = fetch_qld_data()
    except Exception as e:
        error_msg = f"데이터 수집 실패: {e}"
        print(f"❌ {error_msg}")
        send_email(
            subject="❌ QLD 모니터링 에러",
            html_body=f"<html><body style='background:#0a0e17;color:#e2e8f0;padding:2rem;font-family:sans-serif;'>"
                      f"<h2>⚠️ 데이터 수집 오류</h2><p>{error_msg}</p></body></html>",
            dry_run=args.dry_run,
        )
        sys.exit(1)

    close = float(df["Close"].iloc[-1])
    print(f"   최근 종가: ${close:.2f}")
    print(f"   데이터: {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')} ({len(df)}일)")

    # ── 2. 지표 계산 ──
    print("\n📊 지표 계산 중...")
    sma200 = calc_sma200(df)
    deviation = calc_deviation(close, sma200)
    rsi, rsi_type = calc_weekly_rsi(df, now_kst)

    print(f"   SMA200: ${sma200:.2f}")
    print(f"   괴리율: {deviation:+.2f}%")
    print(f"   주봉 RSI(14): {rsi:.1f} [{rsi_type}]")

    # ── 3. 상태 판정 ──
    status = determine_status(deviation, rsi)
    margin = calc_margin_to_stage1(deviation, rsi)

    print(f"\n{status['emoji']} 현재 상태: {status['name']}")
    print(f"   {status['description']}")
    print(f"   1단계까지: 괴리율 {margin['deviation_margin']:.2f}%p / RSI {margin['rsi_margin']:.1f}pt")

    # ── 4. 이력 업데이트 ──
    date_str = now_kst.strftime("%Y-%m-%d")
    history = update_history(date_str, close, sma200, deviation, rsi, status["name"])
    print(f"\n💾 이력 저장 완료 ({len(history)}일치)")

    # ── 5. 이메일 발송 ──
    if weekday == 5:  # 토요일
        subject = f"[확정] QLD 주말 확정 리포트 {status['emoji']} {status['name']}"
    else:
        subject = f"[진행형] QLD 데일리 모니터링 {status['emoji']} {status['name']}"

    email_html = build_email_html(status, close, sma200, deviation, rsi, rsi_type, margin, now_kst)
    send_email(subject, email_html, dry_run=args.dry_run)

    # ── 6. HTML 대시보드 생성 ──
    last_updated = now_kst.strftime("%Y-%m-%d %H:%M KST")

    html_content = generate_dashboard_html(
        status=status, close=close, sma200=sma200,
        deviation=deviation, rsi=rsi, rsi_type=rsi_type,
        margin=margin, history=history,
        last_updated=last_updated, current_year=now_kst.year,
    )

    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n🌐 index.html 생성 완료: {OUTPUT_HTML_PATH}")
    print("\n✅ 모든 작업 완료!")


if __name__ == "__main__":
    main()
