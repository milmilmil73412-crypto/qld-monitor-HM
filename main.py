#!/usr/bin/env python3
"""
ISA K-QLD 비중관리 알림 + QLD/K-QLD 신호(참고용) + 웹 대시보드
==========================================
- 목표 K-SCHD 40 / K-QLD 50 / K-SGOV 10, 계산은 isa.py, 메일은 isa_mail.py, 잔고 입력은 isa_inbox.py
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

import isa
import isa_inbox
import isa_mail


# ─── 상수 ──────────────────────────────────────────────────────────────────────

KST = ZoneInfo("Asia/Seoul")
EST = ZoneInfo("America/New_York")  # "US/Eastern"은 Ubuntu 26 러너에 없음

# 상태 매트릭스 정의
STATUS_LEVELS = [
    {
        "name": "3단계",
        "emoji": "🟣",
        "color": "#b86bff",
        "description": "깊은 하락 — 참고용 (바닥 아님, 규칙상 추가매수 없음)",
        "rsi_threshold": 36,
        "deviation_threshold": -26,
    },
    {
        "name": "2단계",
        "emoji": "🔴",
        "color": "#ff3860",
        "description": "큰 조정 — 참고용 (규칙상 추가매수 없음)",
        "rsi_threshold": 42,
        "deviation_threshold": -18,
    },
    {
        "name": "1단계",
        "emoji": "🟠",
        "color": "#ff6b35",
        "description": "조정 진입 — 참고용 (규칙상 추가매수 없음)",
        "rsi_threshold": 48,
        "deviation_threshold": -10,
    },
]

STATUS_WATCH = {
    "name": "관심",
    "emoji": "🟡",
    "color": "#ffaa00",
    "description": "조정 근처 — 참고용",
}

STATUS_STABLE = {
    "name": "안정",
    "emoji": "🟢",
    "color": "#00d68f",
    "description": "평시 관망 구간",
}

HISTORY_MAX_DAYS = 90

HISTORY_PATH = Path(__file__).parent / "data" / "history.json"
PREVIEW_DIR = Path(__file__).parent / "preview"   # --dry-run 메일 미리보기 (.gitignore)
OUTPUT_HTML_PATH = Path(__file__).parent / "index.html"


# ─── 지표 계산 ─────────────────────────────────────────────────────────────────

def fetch_price_data(symbol: str, period_days: int = 300) -> pd.DataFrame:
    """yfinance에서 일봉 데이터를 가져온다. 종가가 빈(NaN) 행은 제거한다."""
    ticker = yf.Ticker(symbol)
    # 200일선 계산을 위해 충분한 데이터 확보
    df = ticker.history(period=f"{period_days}d", interval="1d")
    if df.empty:
        raise RuntimeError(f"{symbol} 데이터를 가져올 수 없습니다. yfinance API를 확인하세요.")
    # [수정 2026-11] 마지막 행 등에 종가가 NaN으로 오는 경우가 있어 제거 (history.json NaN 기록 원인)
    df = df.dropna(subset=["Close"])
    if df.empty:
        raise RuntimeError(f"{symbol} 데이터에 유효한 종가가 없습니다.")
    return df


def fetch_qld_data(period_days: int = 300) -> pd.DataFrame:
    """QLD 일봉 데이터."""
    return fetch_price_data("QLD", period_days)


def fetch_krx_data(code: str, now_kst: datetime, period_days: int = 300) -> pd.DataFrame:
    """한국 상장 종목 일봉. 한국장 마감(15:40) 전에 실행되면 오늘 행은 장중 가격이므로 버리고 전일 종가를 쓴다."""
    df = fetch_price_data(f"{code}.KS", period_days)
    last_date = df.index[-1].date()
    if last_date == now_kst.date() and (now_kst.hour, now_kst.minute) < (15, 40):
        df = df.iloc[:-1]
    return df


def fetch_kqld_data(now_kst: datetime, period_days: int = 300) -> pd.DataFrame:
    """K-QLD(418660, TIGER 미국나스닥100레버리지(합성)) 일봉 데이터."""
    return fetch_krx_data("418660", now_kst, period_days)


def combine_signal(qld_status: dict, k_status: dict) -> dict:
    """두 기준(QLD 달러 / K-QLD 원화)의 신호를 합쳐 해석한다.
    - 둘 다 1단계 이상: 나스닥 자체가 싸고 원화 기준으로도 싼 상태
    - QLD만: 나스닥은 빠졌지만 원화 약세가 K-QLD 하락을 완충 (0046A0 → K-QLD 교체 판단 기준)
    - K-QLD만: 나스닥보다 환율(원화 강세) 영향이 큰 신호 (신규 원화 자금 판단 기준)
    """
    signal_names = {lvl["name"] for lvl in STATUS_LEVELS}
    q_on = qld_status["name"] in signal_names
    k_on = k_status["name"] in signal_names
    if q_on and k_on:
        return {"code": "both", "label": "둘 다 신호", "emoji": "🔴🔴",
                "desc": "나스닥 자체 하락 + 원화 기준으로도 싼 구간 (참고용, 매수량은 목표비중 규칙대로)"}
    if q_on:
        return {"code": "qld_only", "label": "QLD만 신호", "emoji": "🔴⚪",
                "desc": "나스닥은 빠졌지만 원화 약세가 완충 중 (참고용)"}
    if k_on:
        return {"code": "kqld_only", "label": "K-QLD만 신호", "emoji": "⚪🔴",
                "desc": "나스닥보다 환율(원화 강세) 영향이 큰 신호 (참고용)"}
    return {"code": "none", "label": "신호 없음", "emoji": "⚪⚪", "desc": "두 기준 모두 신호 없음"}


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
      안정: 괴리율 > -5% AND RSI > 55
      관심: 그 외 나머지 전부 (안정도 신호도 아닌 애매한 구간 포함)
    """
    for level in STATUS_LEVELS:
        if rsi <= level["rsi_threshold"] and deviation <= level["deviation_threshold"]:
            return level

    if deviation > -5 and rsi > 55:
        return STATUS_STABLE

    return STATUS_WATCH


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
                   deviation: float, rsi: float, status_name: str,
                   extra: dict | None = None) -> list[dict]:
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
    if extra:
        today_entry.update(extra)

    history = [h for h in history if h.get("date") != date_str]
    history.append(today_entry)
    history.sort(key=lambda x: x["date"])

    save_history(history)
    return history


# ─── 이메일 발송 ───────────────────────────────────────────────────────────────

def build_email_html(status: dict, close: float, sma200: float,
                     deviation: float, rsi: float, rsi_type: str,
                     margin: dict, now_kst: datetime, history: list[dict],
                     kqld: dict | None = None, isa_html: str = "",
                     title: str = "📊 ISA 데일리") -> str:
    """HTML 형태의 이메일 본문을 생성한다."""
    weekday_ko = ["월", "화", "수", "목", "금", "토", "일"][now_kst.weekday()]
    date_str = now_kst.strftime("%Y년 %m월 %d일") + f" ({weekday_ko})"

    # K-QLD(원화 기준) 카드
    kqld_html = ""
    if kqld:
        ks = kqld["status"]
        comb = kqld["combined"]
        kqld_html = f'''<div style="margin:12px 0 20px;padding:18px;border-radius:16px;background:#111827;border:1px solid {ks['color']}66;">
  <div style="font-size:13px;color:#94a3b8;margin-bottom:6px;">K-QLD 418660 (원화 기준)</div>
  <div style="font-size:22px;font-weight:700;color:{ks['color']};">{ks['emoji']} {ks['name']}</div>
  <div style="font-size:13px;color:#cbd5e1;margin-top:6px;">종가 {kqld['close']:,.0f}원 · 200일선 {kqld['sma200']:,.0f}원 · 괴리율 {kqld['deviation']:+.2f}% · 주봉 RSI {kqld['rsi']:.1f} (기준일 {kqld['date']})</div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid #1e293b;font-size:14px;color:#e2e8f0;"><b>종합: {comb['emoji']} {comb['label']}</b></div>
  <div style="font-size:12px;color:#94a3b8;margin-top:4px;">{comb['desc']}</div>
</div>'''

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

    # 최근 30일 데이터 추출 및 퀵차트 URL 생성
    recent = history[-30:] if history else []
    labels = [datetime.strptime(r["date"], "%Y-%m-%d").strftime("%m/%d") for r in recent] if recent else []
    dev_data = [r.get("deviation", 0) for r in recent]
    rsi_data = [r.get("rsi", 0) for r in recent]

    import json
    import urllib.parse
    chart_config = {
        'type': 'line',
        'data': {
            'labels': labels,
            'datasets': [
                {'label': '200일선 괴리율 (%)', 'data': dev_data, 'borderColor': '#ffaa00', 'borderWidth': 2, 'fill': False, 'yAxisID': 'y-dev', 'pointRadius': 0},
                {'label': '주봉 RSI (pt)', 'data': rsi_data, 'borderColor': '#00d68f', 'borderWidth': 2, 'fill': False, 'yAxisID': 'y-rsi', 'pointRadius': 0}
            ]
        },
        'options': {
            'backgroundColor': '#111827',
            'legend': { 'labels': { 'fontColor': '#94a3b8' } },
            'title': { 'display': True, 'text': '최근 30일 모니터링 트렌드', 'fontColor': '#e2e8f0', 'fontSize': 14 },
            'scales': {
                'yAxes': [
                    {'id': 'y-dev', 'position': 'left', 'gridLines': {'color': '#1e293b'}, 'ticks': {'fontColor': '#ffaa00'}},
                    {'id': 'y-rsi', 'position': 'right', 'gridLines': {'display': False}, 'ticks': {'fontColor': '#00d68f'}}
                ],
                'xAxes': [{'gridLines': {'color': '#1e293b'}, 'ticks': {'fontColor': '#94a3b8'}}],
            },
            'annotation': {
                'annotations': [
                    {'type': 'line', 'mode': 'horizontal', 'scaleID': 'y-dev', 'value': -10, 'borderColor': 'rgba(255,56,96,0.8)', 'borderWidth': 1, 'borderDash': [4, 4]},
                    {'type': 'line', 'mode': 'horizontal', 'scaleID': 'y-rsi', 'value': 48, 'borderColor': 'rgba(255,56,96,0.8)', 'borderWidth': 1, 'borderDash': [4, 4]}
                ]
            }
        }
    }
    chart_url = "https://quickchart.io/chart?c=" + urllib.parse.quote(json.dumps(chart_config)) + "&w=600&h=300&bkg=111827" if recent else ""
    chart_html = f'<div style="text-align:center; margin: 20px 0;"><img src="{chart_url}" alt="Trend Chart" style="max-width:100%; border-radius:12px; border:1px solid #1e293b;"></div>' if chart_url else ''

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#0a0e17;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;padding:20px;">

<!-- Header -->
<div style="text-align:center;padding:20px 0;border-bottom:1px solid #1e293b;">
  <h1 style="color:#e2e8f0;font-size:20px;margin:0;">{title}</h1>
  <p style="color:#64748b;font-size:13px;margin:5px 0 0;">{date_str} | {rsi_type} 주봉 RSI</p>
</div>

{isa_html}

<div style="margin:28px 0 0;color:#94a3b8;font-size:13px;font-weight:600;">📊 신호 상세 (참고용 — 매수량은 목표비중 규칙대로)</div>

<!-- 상태 카드 -->
<div style="margin:20px 0;padding:24px;border-radius:16px;background:{bg};border:1px solid {status['color']}44;text-align:center;">
  <div style="font-size:13px;color:#94a3b8;margin-bottom:4px;">QLD (달러 기준)</div>
  <div style="font-size:48px;margin-bottom:8px;">{status['emoji']}</div>
  <div style="font-size:28px;font-weight:700;color:{status['color']};margin-bottom:4px;">{status['name']}</div>
  <div style="font-size:14px;color:#94a3b8;">{status['description']}</div>
</div>

{kqld_html}

{chart_html}

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

<!-- 운용 규칙 -->
<div style="margin:20px 0;padding:16px;background:#111827;border-radius:12px;border:1px solid #1e293b;">
  <div style="color:#94a3b8;font-size:13px;font-weight:600;margin-bottom:8px;">📋 운용 규칙 ("50%로 되돌리기만")</div>
  <div style="color:#64748b;font-size:12px;line-height:1.6;">
    • <b style="color:#e2e8f0;">목표</b>: K-SCHD 40% / K-QLD 50% / K-SGOV 10% (총자산 기준)<br>
    • <b style="color:#e2e8f0;">투자일</b>: 투자금은 목표보다 부족한 종목부터 산다<br>
    • <b style="color:#e2e8f0;">60% 초과</b>: K-QLD가 60%를 넘으면 신호와 상관없이 바로 50%로<br>
    • <b style="color:#e2e8f0;">12월</b>: 5%p 넘게 벗어난 종목만 목표로 (K-QLD 부족분은 예수금 → K-SGOV → K-SCHD)<br>
    • <b style="color:#e2e8f0;">신호</b>: 참고용. 떨어졌다고 50% 넘게 더 사지 않는다
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

    if (not gmail_address or not gmail_password) and not dry_run:
        print("⚠️  GMAIL_ADDRESS 또는 GMAIL_APP_PASSWORD 환경변수가 설정되지 않았습니다.")
        print("   이메일 발송을 건너뜁니다.")
        return

    if dry_run:
        PREVIEW_DIR.mkdir(exist_ok=True)
        n = len(list(PREVIEW_DIR.glob("*.html"))) + 1
        (PREVIEW_DIR / f"{n:02d}.html").write_text(f"<!-- {subject} -->\n" + html_body, encoding="utf-8")
        print(f"\n📧 [DRY-RUN] 이메일 발송 시뮬레이션 → preview/{n:02d}.html")
        print(f"   To: {gmail_address}")
        print(f"   Subject: {subject}")
        print(f"   Body length: {len(html_body)} chars")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = gmail_address  # 자기 자신에게 발송
    msg[isa_inbox.BOT_HEADER] = "1"  # 명령 메일로 다시 읽지 않도록 표시

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
                            last_updated: str, current_year: int,
                            live_prices_json: str = "{}",
                            kqld: dict | None = None) -> str:
    """index.html 대시보드를 생성한다."""
    # K-QLD(원화 기준) 카드 HTML
    if kqld:
        ks = kqld["status"]
        comb = kqld["combined"]
        k_dev_color = "#ff3860" if kqld["deviation"] < -10 else ("#ffaa00" if kqld["deviation"] < -5 else "#00d68f")
        k_rsi_color = "#ff3860" if kqld["rsi"] < 42 else ("#ffaa00" if kqld["rsi"] < 48 else "#00d68f")
        kqld_section = f'''
        <!-- K-QLD (원화 기준) -->
        <div class="glass-card" id="kqld-section" style="margin-bottom:1.5rem;border:1px solid {ks['color']}66;">
            <div class="metric-title">K-QLD 418660 (원화 기준) · 기준일 {kqld['date']}</div>
            <div style="font-size:1.6rem;font-weight:700;color:{ks['color']};margin:0.3rem 0;">{ks['emoji']} {ks['name']}</div>
            <div class="metrics-grid" style="margin:0.8rem 0 0;">
                <div><div class="metric-sub">종가 / 200일선</div><div class="metric-value" style="font-size:1.3rem;">{kqld['close']:,.0f}원</div><div class="metric-sub">SMA200 {kqld['sma200']:,.0f}원</div></div>
                <div><div class="metric-sub">200일선 괴리율</div><div class="metric-value" style="font-size:1.3rem;color:{k_dev_color}">{kqld['deviation']:+.2f}%</div><div class="metric-sub">1단계까지 {kqld['margin']['deviation_margin']:.2f}%p</div></div>
                <div><div class="metric-sub">주봉 RSI</div><div class="metric-value" style="font-size:1.3rem;color:{k_rsi_color}">{kqld['rsi']:.1f}</div><div class="metric-sub">1단계까지 {kqld['margin']['rsi_margin']:.1f}pt</div></div>
            </div>
            <div style="margin-top:1rem;padding-top:0.8rem;border-top:1px solid rgba(148,163,184,0.2);">
                <b>종합: {comb['emoji']} {comb['label']}</b>
                <div class="metric-sub" style="margin-top:0.3rem;">{comb['desc']}</div>
            </div>
        </div>'''
    else:
        kqld_section = '''
        <div class="glass-card" id="kqld-section" style="margin-bottom:1.5rem;"><div class="metric-title">K-QLD 418660 (원화 기준)</div><div class="metric-sub">오늘은 K-QLD 데이터를 가져오지 못했습니다.</div></div>'''
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
            <div class="metric-sub">QLD (달러 기준)</div>
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

{kqld_section}

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

        <!-- QLD 가격 추이 -->
        <div class="glass-card" id="price-chart-section">
            <div class="metric-title" style="margin-bottom:0.5rem;">QLD 종가 추이 (30일)</div>
            <div class="chart-container">
                <canvas id="priceChart"></canvas>
            </div>
            <div class="chart-legend">
                <span><span class="legend-dot" style="background:#3b82f6;"></span> 종가</span>
                <span><span class="legend-dot" style="background:rgba(148,163,184,0.7);"></span> 200일선</span>
            </div>
        </div>

        <!-- 내 포트폴리오 현황 -->
        <div class="glass-card pf-section" id="portfolio-section">
            <div class="metric-title" style="display:flex; justify-content:space-between; align-items:center;">
                <span>💼 내 포트폴리오 현황</span>
                <button class="pf-btn-sm" onclick="pfSyncSettings()" style="background:transparent; border:1px solid #475569; color:#94a3b8; font-size:0.8rem; border-radius:4px; padding:4px 8px; cursor:pointer;">기기 연동 설정</button>
            </div>

            <!-- 종목 추가 폼 -->
            <div class="pf-add-form" style="grid-template-columns:1fr 1fr auto;">
                <div>
                    <label>종목 티커 (심볼)</label>
                    <input type="text" class="pf-input" id="pf-ticker" placeholder="예: 418660, AAPL, CASH(현금)">
                </div>
                <div>
                    <label>보유 수량 (현금은 금액 입력)</label>
                    <input type="number" class="pf-input" id="pf-qty" placeholder="0" min="0" step="any">
                </div>
                <div style="padding-top:1.1rem;">
                    <button class="pf-btn" id="pf-add-btn" onclick="pfAdd()">+ 추가</button>
                </div>
            </div>
            <div id="pf-status-msg" style="font-size:0.8rem; color:var(--status-color); margin-bottom:1rem;"></div>

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
                            <tr><td>🟢 방어·배당</td><td>K-SCHD · 458730 TIGER 미국배당다우존스</td><td>40%</td></tr>
                            <tr><td>🔵 공격</td><td>K-QLD · 418660 TIGER 미국나스닥100레버리지(합성)</td><td>50%</td></tr>
                            <tr><td>💰 대기·현금</td><td>K-SGOV · 0046A0 TIGER 미국초단기국채</td><td>10%</td></tr>
                            <tr><td colspan="3" style="padding-top:1rem;font-size:0.85rem;">운용: ① 투자일마다 투자금은 목표보다 부족한 종목부터 ② K-QLD가 60%를 넘으면 신호와 상관없이 바로 50%로 ③ 매년 12월 5%p 넘게 벗어난 종목만 목표로 (K-QLD 부족분은 예수금 → K-SGOV → K-SCHD). 떨어졌다고 50% 넘게 더 사지 않아요.</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="accordion-item">
                <div class="accordion-header" onclick="toggleAccordion(this)">
                    <span>🚦 신호 가이드 (참고용 — 매수량은 바꾸지 않아요)</span>
                    <svg class="accordion-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </div>
                <div class="accordion-content">
                    <table class="guide-table">
                        <thead><tr><th>상태</th><th>조건</th><th>설명</th></tr></thead>
                        <tbody>
                            <tr><td>🟢 안정</td><td>괴리율 &gt; -5% AND RSI &gt; 55</td><td>평시 관망 구간</td></tr>
                            <tr><td>🟡 관심</td><td>안정·1단계 조건에 모두 해당하지 않는 나머지</td><td>조정 근처 (참고용)</td></tr>
                            <tr><td>🟠 1단계</td><td>RSI ≤ 48 AND 괴리율 ≤ -10%</td><td>조정 진입 (참고용)</td></tr>
                            <tr><td>🔴 2단계</td><td>RSI ≤ 42 AND 괴리율 ≤ -18%</td><td>큰 조정 (참고용)</td></tr>
                            <tr><td>🟣 3단계</td><td>RSI ≤ 36 AND 괴리율 ≤ -26%</td><td>깊은 하락 (참고용, 바닥 아님)</td></tr>
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

        function drawPriceChart() {{
            const canvas = document.getElementById('priceChart');
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
            const pad = {{ top:25, right:20, bottom:35, left:55 }};
            const pW = W - pad.left - pad.right;
            const pH = H - pad.top - pad.bottom;
            const n = chartData.length;

            const closes = chartData.map(d => d.close);
            const smas = chartData.map(d => d.sma200);
            const dates = chartData.map(d => d.date);

            const pMin = Math.min(...closes, ...smas) * 0.97;
            const pMax = Math.max(...closes, ...smas) * 1.03;

            const xOf = i => pad.left + (i / (n - 1)) * pW;
            const yOf = v => pad.top + pH - ((v - pMin) / (pMax - pMin)) * pH;

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

            // 200일선 (점선)
            ctx.beginPath();
            ctx.setLineDash([4, 4]);
            ctx.moveTo(xOf(0), yOf(smas[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yOf(smas[i]));
            ctx.strokeStyle = 'rgba(148,163,184,0.7)'; ctx.lineWidth = 1.5; ctx.stroke();
            ctx.setLineDash([]);

            // 종가 라인 + 영역 fill
            ctx.beginPath();
            ctx.moveTo(xOf(0), yOf(closes[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yOf(closes[i]));
            ctx.lineTo(xOf(n-1), H - pad.bottom); ctx.lineTo(xOf(0), H - pad.bottom); ctx.closePath();
            ctx.fillStyle = 'rgba(59,130,246,0.08)'; ctx.fill();

            ctx.beginPath();
            ctx.moveTo(xOf(0), yOf(closes[0]));
            for (let i = 1; i < n; i++) ctx.lineTo(xOf(i), yOf(closes[i]));
            ctx.strokeStyle = '#3b82f6'; ctx.lineWidth = 2.5; ctx.stroke();

            // 최신 데이터 포인트 dot
            ctx.beginPath(); ctx.arc(xOf(n-1), yOf(closes[n-1]), 4, 0, Math.PI*2);
            ctx.fillStyle = '#3b82f6'; ctx.fill();

            // Y축 라벨 (가격)
            ctx.textAlign = 'right'; ctx.fillStyle = '#8b9bb4'; ctx.font = '10px Inter';
            for (let i = 0; i <= 4; i++) {{
                const v = pMin + (pMax - pMin) * (i / 4);
                ctx.fillText('$' + v.toFixed(0), pad.left - 8, pad.top + pH - (pH / 4) * i + 3);
            }}
        }}

        drawChart();
        drawPriceChart();
        window.addEventListener('resize', drawChart);
        window.addEventListener('resize', drawPriceChart);

        // ══════════════════════════════════════════════
        // ── Portfolio Manager (localStorage) ──
        // ══════════════════════════════════════════════
        const PF_KEY = 'qld_portfolio_v2';
        const SERVER_PRICES = {live_prices_json};
        let usdKrwRate = SERVER_PRICES["USDKRW"] || 1380; // Python에서 수집한 환율

        const PF_COLORS = [
            '#3b82f6','#00d68f','#ffaa00','#ff6b35','#b86bff',
            '#ff3860','#06b6d4','#f43f5e','#84cc16','#a78bfa',
            '#fb923c','#22d3ee','#e879f9','#facc15','#4ade80'
        ];
        // [2026-11] ISA 3종 목표비중 (v6: K-SCHD 40 / K-QLD 50 / K-SGOV 10) — Python TARGET_WEIGHTS와 같게 유지
        const PF_TARGET = [
            {{ ticker:'458730', name:'K-SCHD (458730 TIGER 미국배당다우존스)', pct:40, color:'#00d68f' }},
            {{ ticker:'418660', name:'K-QLD (418660 TIGER 나스닥100레버리지)', pct:50, color:'#3b82f6' }},
            {{ ticker:'0046A0', name:'K-SGOV (0046A0 TIGER 미국초단기국채)', pct:10, color:'#ff6b35' }},
        ];

        function pfLoad() {{
            try {{ return JSON.parse(localStorage.getItem(PF_KEY)) || []; }}
            catch {{ return []; }}
        }}
        function pfSave(h) {{ localStorage.setItem(PF_KEY, JSON.stringify(h)); }}

        async function fetchViaProxy(url) {{
            const proxies = [
                `https://api.allorigins.win/raw?url=${{encodeURIComponent(url)}}`,
                `https://api.codetabs.com/v1/proxy?quest=${{encodeURIComponent(url)}}`
            ];
            for (let proxy of proxies) {{
                try {{
                    const res = await fetch(proxy, {{ cache: 'no-store' }});
                    if (res.ok) return await res.json();
                }} catch(e) {{}}
            }}
            return null;
        }}

        async function fetchNaverInfo(ticker) {{
            let isUS = /^[A-Z]+$/.test(ticker);
            let rawTicker = ticker.replace('.KS','').replace('.KQ','');
            let url = isUS 
                ? `https://api.stock.naver.com/stock/${{rawTicker}}.O/basic`
                : `https://m.stock.naver.com/api/stock/${{rawTicker}}/basic`;
            
            const data = await fetchViaProxy(url);
            if (data && data.closePrice) {{
                let price = parseFloat(data.closePrice.replace(/,/g, ''));
                let currency = 'KRW';
                if (data.stockExchangeType && data.stockExchangeType.nationType !== 'KOR') currency = 'USD';
                if (data.currencyType) currency = data.currencyType.code;
                return {{ price, currency, name: data.stockName || ticker }};
            }}
            return null;
        }}

        async function fetchYahooInfo(ticker) {{
            const url = `https://query1.finance.yahoo.com/v8/finance/chart/${{ticker}}`;
            const data = await fetchViaProxy(url);
            if (data && data.chart && data.chart.result) {{
                const meta = data.chart.result[0].meta;
                return {{ price: meta.regularMarketPrice || meta.chartPreviousClose, currency: meta.currency, name: meta.shortName || meta.longName || ticker }};
            }}
            return null;
        }}

        async function getLivePriceSilent(ticker) {{
            let t = ticker.trim().toUpperCase();
            if (t === 'CASH' || t === '현금' || t === 'CMA') return null;
            if (SERVER_PRICES[t]) {{
                const target = PF_TARGET.find(x => x.ticker === t);
                const name = target ? target.name.replace(t + ' ', '') : t;
                return {{ symbol: t, price: SERVER_PRICES[t], currency: (t === 'DGRO' || t === 'AAPL' || t === 'QQQ' || t === 'SPY') ? 'USD' : 'KRW', name: name }};
            }}

            let naverInfo = await fetchNaverInfo(t);
            if (naverInfo) {{ naverInfo.symbol = t; return naverInfo; }}

            let queryTicker = t;
            if (/^\d{{6}}$/.test(queryTicker)) queryTicker += '.KS';
            let yfInfo = await fetchYahooInfo(queryTicker);
            if (!yfInfo && /^\d{{6}}\.KS$/.test(queryTicker)) {{
                queryTicker = queryTicker.replace('.KS', '.KQ');
                yfInfo = await fetchYahooInfo(queryTicker);
            }}
            if (!yfInfo) yfInfo = await fetchYahooInfo(t);
            
            if(yfInfo) {{ yfInfo.symbol = t; return yfInfo; }}
            return null;
        }}

        // 페이지 로드 시 백그라운드로 가격 및 환율 업데이트
        async function pfInitAutoUpdate() {{
            // 만약 토큰이 있다면 먼저 GitHub에서 최신 데이터를 당겨온다
            if (localStorage.getItem('gh_token')) {{
                await pfSyncPull();
            }}
            
            pfRender();
            
            try {{
                const rateInfo = await fetchYahooInfo('KRW=X');
                if (rateInfo && rateInfo.price) usdKrwRate = rateInfo.price;
                
                const holdings = pfLoad();
                let updated = false;
                for (let h of holdings) {{
                    if (h.ticker === 'CASH' || SERVER_PRICES[h.ticker]) continue;
                    
                    const live = await getLivePriceSilent(h.ticker);
                    if (live && live.price) {{
                        if (h.price !== live.price) updated = true; // 실제 가격 변동시에만
                        h.price = live.price;
                        if (live.currency) h.currency = live.currency;
                        if (live.name) h.name = live.name;
                    }}
                }}
                if (updated) {{
                    pfSave(holdings); // This will also push to GitHub if token exists
                    pfRender();
                }}
            }} catch(e) {{}}
        }}

        async function getLivePrice(ticker) {{
            let t = ticker.trim().toUpperCase();
            if (t === 'CASH' || t === '현금' || t === 'CMA') {{
                return {{ symbol: 'CASH', price: 1, currency: 'KRW', name: '현금' }};
            }}
            
            if (SERVER_PRICES[t]) {{
                const target = PF_TARGET.find(x => x.ticker === t);
                const name = target ? target.name.replace(t + ' ', '') : t;
                return {{ symbol: t, price: SERVER_PRICES[t], currency: (t === 'DGRO' || t === 'AAPL' || t === 'QQQ' || t === 'SPY') ? 'USD' : 'KRW', name: name }};
            }}

            // 1. 네이버 금융 시도 (우회 차단이 적고 한국/미국 주식 모두 지원)
            let naverInfo = await fetchNaverInfo(t);
            if (naverInfo) {{
                naverInfo.symbol = t;
                return naverInfo;
            }}

            // 2. 야후 파이낸스 시도 (최후의 보루)
            let queryTicker = t;
            if (/^\d{{6}}$/.test(queryTicker)) queryTicker += '.KS';
            
            let yfInfo = await fetchYahooInfo(queryTicker);
            if (!yfInfo && /^\d{{6}}\.KS$/.test(queryTicker)) {{
                queryTicker = queryTicker.replace('.KS', '.KQ');
                yfInfo = await fetchYahooInfo(queryTicker);
            }}
            if (!yfInfo) yfInfo = await fetchYahooInfo(t);
            
            if(yfInfo) {{
                yfInfo.symbol = t;
                return yfInfo;
            }}
            
            // 3. 모두 실패 시 수동 입력 창
            const manualPrice = prompt(`'${{t}}'의 현재가를 자동으로 불러오지 못했습니다. (서버 보안 차단)\n보유하신 1주당 단가를 직접 입력해주세요:`);
            if (manualPrice && !isNaN(parseFloat(manualPrice))) {{
                const manualName = prompt(`'${{t}}'의 종목명을 입력해주세요 (예: 삼성전자):`, t);
                const guessedCurrency = /^[A-Z]+$/.test(t) ? 'USD' : 'KRW';
                return {{ symbol: t, price: parseFloat(manualPrice), currency: guessedCurrency, name: manualName || t }};
            }}
            return null;
        }}

        async function pfAdd() {{
            const ticker = document.getElementById('pf-ticker').value.trim();
            const qty = parseFloat(document.getElementById('pf-qty').value);
            if (!ticker || isNaN(qty) || qty <= 0) {{
                alert('종목 티커와 수량을 정확히 입력해주세요.');
                return;
            }}
            
            const btn = document.getElementById('pf-add-btn');
            const msg = document.getElementById('pf-status-msg');
            btn.disabled = true;
            btn.innerText = "조회 중...";
            msg.innerText = "현재가를 불러오는 중입니다...";
            
            const info = await getLivePrice(ticker);
            
            btn.disabled = false;
            btn.innerText = "+ 추가";
            msg.innerText = "";
            
            if (!info) return;
            
            const h = pfLoad();
            const existing = h.find(x => x.ticker === info.symbol);
            if (existing) {{
                existing.qty += qty;
                existing.price = info.price;
                existing.currency = info.currency;
                if (info.name) existing.name = info.name;
            }} else {{
                h.push({{ 
                    id: Date.now(), 
                    ticker: info.symbol, 
                    qty: qty, 
                    price: info.price,
                    currency: info.currency,
                    name: info.name || info.symbol
                }});
            }}
            pfSave(h);
            document.getElementById('pf-ticker').value = '';
            document.getElementById('pf-qty').value = '';
            pfRender();
        }}

        function pfRemove(id) {{
            pfSave(pfLoad().filter(x => x.id !== id));
            pfRender();
        }}

        function pfFmt(n) {{
            if (n >= 1e8) return (n/1e8).toFixed(1) + '억';
            if (n >= 1e4) return (n/1e4).toFixed(0) + '만';
            return Math.round(n).toLocaleString();
        }}

        function pfRender() {{
            const holdings = pfLoad();
            const wrap = document.getElementById('pf-table-wrap');
            const visuals = document.getElementById('pf-visuals');

            if (holdings.length === 0) {{
                wrap.innerHTML = '<div class="pf-empty">종목 티커와 수량을 추가하면 현재가를 자동으로 불러와 배분 현황을 보여줍니다.</div>';
                visuals.style.display = 'none';
                return;
            }}

            let totalKrw = 0;
            holdings.forEach(h => {{ 
                let krwPrice = h.price;
                if (h.currency === 'USD') krwPrice = h.price * usdKrwRate;
                h.krwValue = h.qty * krwPrice; 
                totalKrw += h.krwValue; 
            }});

            let rows = holdings.map((h, i) => {{
                const pct = totalKrw > 0 ? (h.krwValue / totalKrw * 100).toFixed(1) : '0.0';
                const c = PF_COLORS[i % PF_COLORS.length];
                const priceDisp = h.currency === 'USD' ? `$${{h.price.toFixed(2)}}` : `₩${{h.price.toLocaleString()}}`;
                
                let dispName = h.name && h.name !== h.ticker ? `${{h.name}} (${{h.ticker}})` : h.ticker;
                if (h.ticker === 'CASH') dispName = '현금 (CMA)';

                return `<tr>
                    <td><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${{c}};margin-right:6px;"></span>${{dispName}}</td>
                    <td style="text-align:right;">${{h.qty.toLocaleString()}}</td>
                    <td style="text-align:right;">${{h.ticker === 'CASH' ? '-' : priceDisp}}</td>
                    <td style="text-align:right;">₩${{pfFmt(h.krwValue)}}</td>
                    <td style="text-align:right;color:var(--text-secondary);">${{pct}}%</td>
                    <td style="text-align:right;white-space:nowrap;">
                        <button class="pf-btn-sm" onclick="pfRemove(${{h.id}})">삭제</button>
                    </td>
                </tr>`;
            }}).join('');

            wrap.innerHTML = `<table class="pf-table">
                <thead><tr><th>종목 (티커)</th><th style="text-align:right;">수량</th><th style="text-align:right;">현재가</th><th style="text-align:right;">평가액 (원)</th><th style="text-align:right;">비중</th><th></th></tr></thead>
                <tbody>${{rows}}
                <tr class="pf-total-row"><td colspan="3">합계</td><td style="text-align:right;">₩${{pfFmt(totalKrw)}}</td><td colspan="2"></td></tr>
                </tbody></table>`;

            visuals.style.display = 'grid';
            pfDrawDonut(holdings, totalKrw);
            pfDrawCompare(holdings, totalKrw);
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
                const pct = total > 0 ? h.krwValue / total : 0;
                const sweep = pct * Math.PI * 2;
                ctx.beginPath();
                ctx.arc(cx, cy, R, angle, angle + sweep);
                ctx.arc(cx, cy, r, angle + sweep, angle, true);
                ctx.closePath();
                ctx.fillStyle = PF_COLORS[i % PF_COLORS.length];
                ctx.fill();
                angle += sweep;
            }});

            ctx.fillStyle = '#f1f5f9'; ctx.font = '700 16px Inter'; ctx.textAlign = 'center';
            ctx.fillText('₩' + pfFmt(total), cx, cy - 2);
            ctx.fillStyle = '#8b9bb4'; ctx.font = '11px Inter';
            ctx.fillText('총 자산', cx, cy + 16);
        }}

        function pfDrawCompare(holdings, total) {{
            const container = document.getElementById('pf-compare');
            const currentMap = {{}};
            holdings.forEach(h => {{
                const key = h.ticker;
                currentMap[key] = (currentMap[key] || 0) + (total > 0 ? h.krwValue / total * 100 : 0);
            }});

            let html = '';
            const maxPct = 100;

            PF_TARGET.forEach(t => {{
                let currentPct = currentMap[t.ticker] || 0;
                delete currentMap[t.ticker];

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

        pfInitAutoUpdate();
    </script>
</body>
</html>"""


# ─── ISA 비중관리 (isa.py / isa_mail.py / isa_inbox.py) ────────────────────────
# ⚠️ 공개 저장소라 Actions 로그도 공개된다. 보유 수량·금액·비중은 print 하지 않는다.

def fetch_isa_prices(now_kst: datetime) -> tuple[dict, dict, dict]:
    """3종목 최근 종가 / 전일 종가 / 1주 전 종가 (원)."""
    prices, prev, week = {}, {}, {}
    for code in isa.CODES:
        try:
            df = fetch_krx_data(code, now_kst, period_days=40)
        except Exception as e:
            print(f"⚠️  {code} 시세 실패: {e}")
            continue
        c = df["Close"]
        prices[code] = float(c.iloc[-1])
        if len(c) >= 2:
            prev[code] = float(c.iloc[-2])
        old = c[c.index.date <= (c.index[-1] - timedelta(days=7)).date()]
        if len(old):
            week[code] = float(old.iloc[-1])
    return prices, prev, week


def fetch_market(df_k: pd.DataFrame | None) -> dict:
    m = {"ndx_from_high": None, "usdkrw": None, "kqld_from_high": None}
    try:
        ndx = fetch_price_data("^NDX", 400)["Close"].iloc[-252:]
        m["ndx_from_high"] = (float(ndx.iloc[-1]) / float(ndx.max()) - 1) * 100
    except Exception as e:
        print(f"⚠️  나스닥100 실패: {e}")
    try:
        m["usdkrw"] = float(fetch_price_data("KRW=X", 10)["Close"].iloc[-1])
    except Exception as e:
        print(f"⚠️  환율 실패: {e}")
    if df_k is not None and len(df_k):
        k = df_k["Close"].iloc[-252:]
        m["kqld_from_high"] = (float(k.iloc[-1]) / float(k.max()) - 1) * 100
    return m


def make_ctx(today, holdings, prices, prev=None, week=None, status=None, kqld=None, market=None) -> dict:
    has = isa_inbox.has_balance(holdings)
    return {
        "today": today, "holdings": holdings or {}, "has_balance": has,
        "pf": isa.valuation(holdings, prices) if has else None,
        "prices": prices, "prev_prices": prev or {}, "week_prices": week or {},
        "qld_status": status, "k_status": kqld["status"] if kqld else None,
        "combined": kqld["combined"] if kqld else None, "market": market or {},
    }


def send_inbox_events(events: list[dict], holdings: dict | None, now_kst: datetime, dry_run: bool) -> None:
    """메일 명령 처리 결과(잔고 인식, 수정, 투자금 변경, 오류)를 메일로 알린다."""
    if not events:
        return
    prices, _, _ = fetch_isa_prices(now_kst)
    ctx = make_ctx(now_kst.date(), holdings, prices)
    for ev in events:
        if ev["kind"] in ("balance", "fix") and ctx["has_balance"]:
            subj, html = isa_mail.confirm_email(ctx["holdings"], ev, ctx["pf"])
        elif ev["kind"] == "contrib":
            subj, html = isa_mail.payday_email(ctx, ev["amount"], ev["invest_day"], changed=True)
        else:
            subj, html = isa_mail.error_email(ev.get("msg", "알 수 없는 오류"))
        send_email(subj, html, dry_run=dry_run)


def run_inbox(now_kst: datetime, dry_run: bool) -> dict | None:
    holdings = isa_inbox.load_holdings()
    holdings, events = isa_inbox.process_inbox(holdings, dry_run)
    send_inbox_events(events, holdings, now_kst, dry_run)
    return holdings


def run_payday(now_kst: datetime, dry_run: bool) -> None:
    """월급날 저녁: '내일 살 것' 배분표. 월급날이 아니면 아무것도 하지 않는다."""
    today = now_kst.date()
    pay = isa.payday(today.year, today.month)
    if today != pay:
        print(f"ℹ️  오늘은 월급날이 아님 (이번 달 월급날 {pay}) → 종료")
        return
    holdings = run_inbox(now_kst, dry_run)
    inv = isa.invest_day(today.year, today.month)
    prices, _, _ = fetch_isa_prices(now_kst)
    ctx = make_ctx(today, holdings, prices)
    subj, html = isa_mail.payday_email(ctx, isa.contribution_for(holdings, inv), inv)
    send_email(subj, html, dry_run=dry_run)


# ─── 메인 실행 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ISA K-QLD 비중관리 + QLD/K-QLD 신호 알림")
    parser.add_argument("--dry-run", action="store_true",
                        help="이메일 발송 없이 테스트 실행 (메일은 preview/ 폴더에 저장)")
    parser.add_argument("--mode", choices=["daily", "payday", "inbox"], default="daily",
                        help="daily: 데일리/토요일 주간 메일 + 대시보드, payday: 월급날 저녁 배분표, inbox: 명령 메일 확인")
    parser.add_argument("--as-of", help="(시험용, --dry-run 전용) 오늘 날짜를 YYYY-MM-DD로 가정")
    args = parser.parse_args()

    now_kst = datetime.now(KST)
    if args.as_of:
        if not args.dry_run:
            parser.error("--as-of 는 --dry-run 과 함께만 쓸 수 있어요")
        d = datetime.fromisoformat(args.as_of)
        now_kst = now_kst.replace(year=d.year, month=d.month, day=d.day)
    weekday = now_kst.weekday()

    print(f"🕐 실행 시간: {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')} · 모드 {args.mode}")
    print(f"   요일: {['월','화','수','목','금','토','일'][weekday]}요일")

    if args.mode == "inbox":
        run_inbox(now_kst, args.dry_run)
        return
    if args.mode == "payday":
        run_payday(now_kst, args.dry_run)
        return

    # ── 0. 명령 메일 먼저 반영 (잔고 캡처, 수정, 투자금 변경) ──
    holdings = run_inbox(now_kst, args.dry_run)

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

    # ── 3.2 K-QLD(418660, 원화 기준) 지표 — 실패해도 QLD 알림은 계속 진행 ──
    kqld = None
    df_k = None
    try:
        df_k = fetch_kqld_data(now_kst)
        k_close = float(df_k["Close"].iloc[-1])
        k_sma200 = calc_sma200(df_k)
        k_dev = calc_deviation(k_close, k_sma200)
        k_rsi, _ = calc_weekly_rsi(df_k, now_kst)
        k_status = determine_status(k_dev, k_rsi)
        kqld = {
            "date": df_k.index[-1].strftime("%Y-%m-%d"),
            "close": k_close, "sma200": k_sma200, "deviation": k_dev, "rsi": k_rsi,
            "status": k_status,
            "combined": combine_signal(status, k_status),
            "margin": calc_margin_to_stage1(k_dev, k_rsi),
        }
        print(f"\n🇰🇷 K-QLD(418660): {k_close:,.0f}원 / SMA200 {k_sma200:,.0f}원 / 괴리율 {k_dev:+.2f}% / RSI {k_rsi:.1f} → {k_status['emoji']} {k_status['name']}")
        print(f"   종합: {kqld['combined']['emoji']} {kqld['combined']['label']}")
    except Exception as e:
        print(f"⚠️  K-QLD 지표 계산 실패 (QLD 알림은 계속): {e}")

    # ── 3.5 3종목 시세 + 시장 (메일·대시보드 계산기용) ──
    print("\n📡 3종목 시세 / 시장 데이터 수집 중...")
    prices, prev_prices, week_prices = fetch_isa_prices(now_kst)
    market = fetch_market(df_k)
    live_prices = dict(prices)
    live_prices["USDKRW"] = market["usdkrw"] or 1380.0

    # ── 3.6 ISA 할 일 (보유 수량은 로그에 남기지 않음) ──
    ctx = make_ctx(now_kst.date(), holdings, prices, prev_prices, week_prices, status, kqld, market)
    todo = isa_mail.todos(ctx)
    print(f"⚖️  ISA 할 일 {len(todo)}건 (내용은 메일로만)")

    # ── 4. 이력 업데이트 ──
    date_str = now_kst.strftime("%Y-%m-%d")
    extra = None
    if kqld:
        extra = {
            "k_date": kqld["date"],
            "k_close": round(kqld["close"], 0),
            "k_sma200": round(kqld["sma200"], 0),
            "k_deviation": round(kqld["deviation"], 2),
            "k_rsi": round(kqld["rsi"], 1),
            "k_status": kqld["status"]["name"],
            "combined": kqld["combined"]["code"],
        }
    history = update_history(date_str, close, sma200, deviation, rsi, status["name"], extra)
    print(f"💾 이력 저장 완료 ({len(history)}일치)")

    # ── 5. 이메일 발송: 평일 데일리, 토요일 주간 요약, 일요일 없음 ──
    if weekday == 6:
        print("ℹ️  일요일은 메일을 보내지 않음")
    else:
        if weekday == 5:
            subject, isa_html = isa_mail.weekly_email(ctx, todo)
            title = "🗓️ ISA 주간 요약"
        else:
            subject = isa_mail.daily_subject(ctx, todo)
            isa_html = isa_mail.build_isa_section(ctx, todo)
            title = "📊 ISA 데일리"
        email_html = build_email_html(status, close, sma200, deviation, rsi, rsi_type, margin, now_kst, history, kqld,
                                      isa_html=isa_html, title=title)
        send_email(subject, email_html, dry_run=args.dry_run)

    # ── 6. HTML 대시보드 생성 (공개 페이지: 보유 수량·금액은 넣지 않음) ──
    last_updated = now_kst.strftime("%Y-%m-%d %H:%M KST")

    html_content = generate_dashboard_html(
        status=status, close=close, sma200=sma200,
        deviation=deviation, rsi=rsi, rsi_type=rsi_type,
        margin=margin, history=history,
        last_updated=last_updated, current_year=now_kst.year,
        live_prices_json=json.dumps(live_prices),
        kqld=kqld,
    )

    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"\n🌐 index.html 생성 완료: {OUTPUT_HTML_PATH}")
    print("\n✅ 모든 작업 완료!")


if __name__ == "__main__":
    main()
