"""
Чек-лист входа в long strangle: 9 параметров на основе имеющихся данных.
История: 7 дней. Экспирация берётся из секции «Расчёт индикаторов» (gex_presets).
"""
import logging
import math
from datetime import date
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# Окно истории в днях
CHECKLIST_LOOKBACK_DAYS = 7
CHECKLIST_HOURS = 24 * CHECKLIST_LOOKBACK_DAYS

# Пороги в долях (vol pts: 1 pt = 0.01)
D_ATM_IV_PUMP_MAX = 0.03   # ΔATM IV(1h) <= 3 vol pts — не в пампе
D_ATM_IV_MIN = -0.015      # -1.5 vol pts
D_ATM_IV_MAX = 0.025       # +2.5 vol pts
RR25_RANGE = 0.02          # RR25 в [-2, +2] vol pts
D_RR25_MIN = 0.01         # |ΔRR25(1h)| > 1.0 vol pt для "роста"
BF25_PERCENTILE_MAX = 75   # BF25 ниже 75 перцентиля за 7 дней
IV_ATM_PERCENTILE_LOW = 30
IV_ATM_PERCENTILE_HIGH = 65
BE_VS_MOVE_MAX_RATIO = 1.5  # BE не дальше 1.5x expected move


def _get_iv_from_row(row: Dict) -> Optional[float]:
    v = row.get("iv") or row.get("mark_iv") or row.get("ask_iv") or row.get("bid_iv")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _delta_abs(d: Any) -> float:
    if d is None:
        return -1.0
    try:
        x = float(d)
        return abs(x)
    except (TypeError, ValueError):
        return -1.0


def _compute_rr25_bf25_from_rows(
    rows: List[Dict],
    iv_atm: Optional[float],
    underlying_price: Optional[float]
) -> Tuple[Optional[float], Optional[float]]:
    """RR25 = IV(25ΔC) - IV(25ΔP), BF25 = 0.5*(IV_25C+IV_25P) - IV_ATM. Delta в долях (0.25)."""
    if not rows or iv_atm is None:
        return None, None
    calls = [r for r in rows if (r.get("option_type") or "").upper() == "C" and _get_iv_from_row(r) is not None]
    puts = [r for r in rows if (r.get("option_type") or "").upper() == "P" and _get_iv_from_row(r) is not None]
    # 25Δ: delta ~ 0.25 (или 25 в процентах — проверяем оба)
    call_25 = None
    put_25 = None
    for r in calls:
        delta = r.get("delta")
        if delta is None:
            continue
        try:
            d = float(delta)
            if 0.2 <= d <= 0.3 or (2 <= abs(d) <= 30 and d > 0):  # 0.25 или 25
                if d > 1:
                    d = d / 100.0
                if abs(d - 0.25) < 0.1:
                    call_25 = _get_iv_from_row(r)
                    break
        except (TypeError, ValueError):
            continue
    for r in puts:
        delta = r.get("delta")
        if delta is None:
            continue
        try:
            d = float(delta)
            if -0.3 <= d <= -0.2 or (-30 <= d <= -2):  # -0.25 или -25
                if d < -1 or d > 1:
                    d = d / 100.0 if abs(d) > 1 else d
                if abs(d + 0.25) < 0.1:
                    put_25 = _get_iv_from_row(r)
                    break
        except (TypeError, ValueError):
            continue
    if call_25 is None or put_25 is None:
        return None, None
    rr25 = call_25 - put_25
    bf25 = 0.5 * (call_25 + put_25) - iv_atm
    return rr25, bf25


def _percentile_value(sorted_values: List[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return sorted_values[min(int(k), len(sorted_values) - 1)]
    return sorted_values[f] + (k - f) * (sorted_values[min(c, len(sorted_values) - 1)] - sorted_values[f])


def run_entry_checklist(
    underlying: str,
    expiration_str: str,
    all_data: Dict[str, Dict],
    db: Any,
) -> Tuple[List[Tuple[str, bool]], int, str]:
    """
    Выполнить чек-лист входа по одной экспирации.
    Returns:
        ([(label, passed), ...], score_0_9, interpretation)
    """
    from core.strategy.gex_calculator import (
        compute_iv_atm_from_board,
        calculate_gex_by_strike,
        gex_zone_and_flip,
        options_from_datastore_for_gex,
    )

    results: List[Tuple[str, bool]] = []
    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"

    # Текущие данные доски
    current_iv_atm = compute_iv_atm_from_board(all_data, underlying, expiration_str)
    underlying_price = None
    for sym, d in all_data.items():
        if sym.startswith(prefix) and d.get("underlying_price") is not None:
            underlying_price = d.get("underlying_price")
            try:
                underlying_price = float(underlying_price)
                break
            except (TypeError, ValueError):
                pass

    # IV_ATM 1h ago
    iv_series = db.get_iv_atm_hourly(underlying, expiration_str, hours=2)
    iv_atm_1h_ago = None
    if len(iv_series) >= 1:
        iv_atm_1h_ago = iv_series[-1][1] if len(iv_series) == 1 else iv_series[-2][1]

    # 1) ATM IV не в пампе
    d_atm_iv = (current_iv_atm - iv_atm_1h_ago) if (current_iv_atm is not None and iv_atm_1h_ago is not None) else None
    pass1 = d_atm_iv is not None and d_atm_iv <= D_ATM_IV_PUMP_MAX
    results.append(("ATM IV не в пампе (Δ1h ≤ 3 vol pts)", pass1))

    # 2) ATM IV в рабочем диапазоне
    pass2 = d_atm_iv is not None and D_ATM_IV_MIN <= d_atm_iv <= D_ATM_IV_MAX
    results.append(("ATM IV в рабочем диапазоне (-1.5…+2.5 vol pts)", pass2))

    # 3) ATM IV в 30–65 перцентиле за 7 дней (по похожим опционам: underlying + DTE)
    pass3 = False
    if current_iv_atm is not None:
        exp_date = db.parse_expiration_date(expiration_str.upper())
        if exp_date is not None:
            dte = (exp_date - date.today()).days
            if dte >= 0:
                stats = db.get_iv_statistics_by_similar_options(
                    underlying_ticker=underlying,
                    days_to_expiration=dte,
                    days=CHECKLIST_LOOKBACK_DAYS,
                    current_iv=current_iv_atm,
                )
                p30 = stats.get("p30")
                p65 = stats.get("p65")
                if p30 is not None and p65 is not None:
                    pass3 = p30 <= current_iv_atm <= p65
    results.append((f"ATM IV в 30–65 перцентиле за {CHECKLIST_LOOKBACK_DAYS} дн.", pass3))

    # Текущая доска для RR25/BF25 (список строк в формате для _compute_rr25_bf25_from_rows)
    board_rows = []
    for sym, d in all_data.items():
        if not sym.startswith(prefix):
            continue
        strike = None
        for part in sym.split("-"):
            try:
                strike = float(part)
                break
            except ValueError:
                continue
        opt_type = "C" if "-C-" in sym.upper() else "P"
        iv = _get_iv_from_row(d)
        if strike is not None and iv is not None:
            board_rows.append({
                "option_type": opt_type,
                "iv": iv,
                "delta": d.get("delta"),
                "strike": strike,
                "underlying_price": d.get("underlying_price"),
            })
    rr25_now, bf25_now = _compute_rr25_bf25_from_rows(board_rows, current_iv_atm, underlying_price)

    # 4) RR25 в норме [-2, +2]. В долях: [-0.02, 0.02]
    pass4 = rr25_now is not None and -RR25_RANGE <= rr25_now <= RR25_RANGE
    results.append(("RR25 в норме [-2…+2 vol pts]", pass4))

    # 5) Рост асимметричного риска |ΔRR25(1h)| > 1.0. Благоприятно = True когда выполняется
    snapshots_2h = db.get_option_snapshots_hourly(underlying, expiration_str, hours=2)
    rr25_1h_ago = None
    if len(snapshots_2h) >= 1:
        hours_sorted = sorted(snapshots_2h.keys())
        hour_1h_ago = hours_sorted[-2] if len(hours_sorted) >= 2 else hours_sorted[-1]
        rows_1h = snapshots_2h[hour_1h_ago]
        iv_atm_1h = db._iv_atm_from_snapshot_rows(rows_1h)
        rr25_1h_ago, _ = _compute_rr25_bf25_from_rows(rows_1h, iv_atm_1h, None)
    d_rr25 = (rr25_now - rr25_1h_ago) if (rr25_now is not None and rr25_1h_ago is not None) else None
    pass5 = d_rr25 is not None and abs(d_rr25) >= D_RR25_MIN
    results.append(("Рост асимметричного риска |ΔRR25(1h)| > 1 vol pt", pass5))

    # 6) BF25 не в экстремуме (ниже 75 перцентиля за 7 дней) — по похожим опционам (underlying + DTE)
    bf25_history: List[float] = []
    exp_date_6 = db.parse_expiration_date(expiration_str.upper())
    if exp_date_6 is not None:
        dte_6 = (exp_date_6 - date.today()).days
        if dte_6 >= 0:
            snapshots_7d = db.get_option_snapshots_hourly_by_dte(
                underlying_ticker=underlying,
                days_to_expiration=dte_6,
                hours=CHECKLIST_HOURS,
            )
            for hour_ts, rows in snapshots_7d.items():
                iv_atm_h = db._iv_atm_from_snapshot_rows(rows)
                if iv_atm_h is None:
                    continue
                rr, bf = _compute_rr25_bf25_from_rows(rows, iv_atm_h, None)
                if bf is not None:
                    bf25_history.append(bf)
    pass6 = False
    if bf25_now is not None and len(bf25_history) >= 5:
        p75 = _percentile_value(sorted(bf25_history), BF25_PERCENTILE_MAX)
        if p75 is not None:
            pass6 = bf25_now < p75
    results.append((f"BF25 не в экстремуме (ниже 75% за {CHECKLIST_LOOKBACK_DAYS} дн.)", pass6))

    # 7) Цена в зоне -GEX или у gamma flip
    opts_gex = options_from_datastore_for_gex(all_data, underlying, expiration_str)
    gex_by_strike = calculate_gex_by_strike(opts_gex) if opts_gex else {}
    spot = underlying_price or 0.0
    in_neg_zone, strong_pos = gex_zone_and_flip(gex_by_strike, spot)
    pass7 = in_neg_zone
    results.append(("Цена в зоне -GEX или у gamma flip", pass7))

    # 8) Нет сильного +GEX вокруг спота
    pass8 = not strong_pos
    results.append(("Нет сильного +GEX вокруг спота", pass8))

    # 9) BE реалистичны относительно expected move. 20Δ call/put, mark_price
    pass9 = False
    if underlying_price and current_iv_atm and current_iv_atm > 0:
        # Найти опционы с delta ~ 0.2 и -0.2
        call_20 = None
        put_20 = None
        for sym, d in all_data.items():
            if not sym.startswith(prefix):
                continue
            delta = d.get("delta")
            if delta is None:
                continue
            try:
                x = float(delta)
                if x > 1 or x < -1:
                    x = x / 100.0
                mark = d.get("mark_price")
                if mark is None:
                    mark = d.get("ask_price") or d.get("bid_price")
                if mark is None:
                    continue
                mark = float(mark)
                strike = None
                for part in sym.split("-"):
                    try:
                        strike = float(part)
                        break
                    except ValueError:
                        continue
                if strike is None:
                    continue
                if 0.15 <= x <= 0.3 and call_20 is None:
                    call_20 = (strike, mark)
                if -0.3 <= x <= -0.15 and put_20 is None:
                    put_20 = (strike, mark)
            except (TypeError, ValueError):
                continue
        if call_20 and put_20:
            k_call, prem_call = call_20
            k_put, prem_put = put_20
            total_prem = prem_call + prem_put
            be_up = k_call + total_prem
            be_down = k_put - total_prem
            expected_move = underlying_price * current_iv_atm / math.sqrt(365)
            if expected_move > 0:
                pass9 = (
                    (be_up - underlying_price) <= BE_VS_MOVE_MAX_RATIO * expected_move
                    and (underlying_price - be_down) <= BE_VS_MOVE_MAX_RATIO * expected_move
                )
    results.append(("BE реалистичны относительно expected move", pass9))

    score = sum(1 for _, p in results if p)
    if score <= 3:
        interpretation = "пропуск"
    elif score <= 6:
        interpretation = "половинный размер"
    else:
        interpretation = "полный вход"

    return results, score, interpretation
