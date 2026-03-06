"""
Расчёт индикатора GEX (Gamma Exposure).

GEX = (open_interest_calls * gamma_calls) - (open_interest_puts * gamma_puts)
по каждому страйку. Учитываются только дневные опционы (days_to_expiration <= 3).
"""
import io
import logging
from typing import Dict, List, Optional, Tuple, Sequence
from collections import defaultdict

logger = logging.getLogger(__name__)

# Максимальный DTE для учёта в GEX (дневные опционы)
GEX_MAX_DTE = 5


def _parse_symbol(symbol: str) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """Извлечь (underlying, expiry_str, strike) из символа. expiry_str например 4JAN26."""
    try:
        parts = symbol.split('-')
        if len(parts) >= 4:
            return parts[0], parts[1], float(parts[2])
    except (ValueError, IndexError):
        pass
    return None, None, None


def _option_type_from_symbol(symbol: str) -> Optional[str]:
    """'C' или 'P' из символа."""
    try:
        parts = symbol.split('-')
        if len(parts) >= 4:
            return parts[3].upper()
    except IndexError:
        pass
    return None


def calculate_gex_by_strike(
    options: List[Dict],
    max_dte: int = GEX_MAX_DTE
) -> Dict[float, float]:
    """
    Рассчитать GEX по страйкам.
    
    Формула по страйку: GEX_strike = (OI_call * gamma_call) - (OI_put * gamma_put).
    Опционы с days_to_expiration > max_dte игнорируются (если поле есть).
    
    Args:
        options: Список словарей с ключами:
            - symbol (или strike + option_type)
            - open_interest (или 0)
            - gamma (или 0)
            - days_to_expiration (опционально; если > max_dte — опцион не учитывается)
        max_dte: Максимальный days_to_expiration для учёта (по умолчанию 3).
    
    Returns:
        Словарь {strike: gex_value}. Страйки отсортированы.
    """
    # По страйку: (oi_call * gamma_call, oi_put * gamma_put)
    call_contrib: Dict[float, float] = defaultdict(float)
    put_contrib: Dict[float, float] = defaultdict(float)
    
    for opt in options:
        dte = opt.get('days_to_expiration')
        if dte is not None and dte > max_dte:
            continue
        symbol = opt.get('symbol', '')
        strike = opt.get('strike')
        if strike is None and symbol:
            _, _, strike = _parse_symbol(symbol)
        if strike is None:
            continue
        oi = float(opt.get('open_interest') or 0)
        gamma = float(opt.get('gamma') or 0)
        contrib = oi * gamma
        opt_type = opt.get('option_type') or _option_type_from_symbol(symbol)
        if opt_type == 'C':
            call_contrib[strike] += contrib
        elif opt_type == 'P':
            put_contrib[strike] += contrib
    
    strikes = sorted(set(call_contrib.keys()) | set(put_contrib.keys()))
    return {s: call_contrib[s] - put_contrib[s] for s in strikes}


def total_gex(gex_by_strike: Dict[float, float]) -> float:
    """Суммарный GEX по всем страйкам."""
    return sum(gex_by_strike.values())


def max_abs_gex(gex_by_strike: Dict[float, float]) -> Tuple[float, Optional[float]]:
    """
    Максимальное отклонение GEX от нуля по модулю по всем страйкам.
    Returns:
        (max_abs_value, strike_at_max) — модуль и страйк, на котором достигнут максимум.
        При пустом словаре — (0.0, None).
    """
    if not gex_by_strike:
        return (0.0, None)
    best_strike = max(gex_by_strike.keys(), key=lambda s: abs(gex_by_strike[s]))
    return (abs(gex_by_strike[best_strike]), best_strike)


def gex_zone_and_flip(
    gex_by_strike: Dict[float, float],
    spot: float,
    near_flip_pct: float = 0.02,
    strong_positive_ratio: float = 0.3
) -> Tuple[bool, bool]:
    """
    Оценка зоны GEX относительно спота для чек-листа входа.
    Returns:
        (in_negative_zone_or_near_flip, strong_positive_around_spot)
        - True, False: благоприятно для волатильности (цена в -GEX или у flip, нет сильного +GEX)
        - False, True: неблагоприятно (сильный +GEX вокруг спота)
    """
    if not gex_by_strike or spot <= 0:
        return (False, False)
    strikes = sorted(gex_by_strike.keys())
    # Gamma flip: уровень, где кумулятивный GEX переходит из - в + (снизу вверх по страйкам)
    cum = 0.0
    flip_strike: Optional[float] = None
    for s in strikes:
        prev = cum
        cum += gex_by_strike[s]
        if prev < 0 and cum >= 0:
            flip_strike = s
            break
    # Благоприятно: спот в зоне -GEX (ниже flip) или рядом с flip
    in_negative_or_near_flip = False
    if flip_strike is not None:
        if spot <= flip_strike:
            in_negative_or_near_flip = True
        elif flip_strike > 0 and spot <= flip_strike * (1 + near_flip_pct):
            in_negative_or_near_flip = True
    else:
        # Flip не найден: кумулятив нигде не перешёл из - в +. Если в точке спота кумулятив < 0 — мы в зоне -GEX.
        cum_at_spot = sum(gex_by_strike[s] for s in strikes if s <= spot)
        if cum_at_spot < 0:
            in_negative_or_near_flip = True
    # Сильный +GEX вокруг спота: GEX в страйках рядом с спотом существенно положительный
    strong_positive = False
    if strikes:
        nearest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        gex_at_spot = gex_by_strike[strikes[nearest_idx]]
        max_gex = max(gex_by_strike.values()) if gex_by_strike else 0
        if max_gex > 0 and gex_at_spot >= strong_positive_ratio * max_gex:
            strong_positive = True
    return (in_negative_or_near_flip, strong_positive)


def options_from_datastore_for_gex(
    data_by_symbol: Dict[str, Dict],
    underlying: str,
    expiration_str: str,
    max_dte: int = GEX_MAX_DTE
) -> List[Dict]:
    """
    Собрать из хранилища (data_store) опционы по underlying и дате экспирации,
    добавить days_to_expiration и отфильтровать по max_dte.
    
    Args:
        data_by_symbol: Словарь {symbol: data} из data_store.get_by_underlying() или .get_all()
        underlying: Базовый актив (BTC, ETH, SOL)
        expiration_str: Дата экспирации в формате Bybit, например "4JAN26"
        max_dte: Максимальный DTE для включения (по умолчанию 3)
    
    Returns:
        Список словарей с ключами symbol, open_interest, gamma, days_to_expiration, option_type
    """
    from datetime import date
    from core.data.option_board import parse_expiration_date
    
    exp_date = parse_expiration_date(expiration_str.upper())
    if not exp_date:
        return []
    today = date.today()
    dte = (exp_date - today).days
    if dte > max_dte:
        return []
    
    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"
    out = []
    for symbol, data in data_by_symbol.items():
        if not symbol.startswith(prefix):
            continue
        oi = data.get('open_interest') or 0
        gamma = data.get('gamma') or 0
        parts = symbol.split('-')
        opt_type = parts[3] if len(parts) >= 4 else None
        out.append({
            'symbol': symbol,
            'open_interest': oi,
            'gamma': gamma,
            'days_to_expiration': dte,
            'option_type': opt_type,
        })
    return out


def gex_summary_for_agent(gex_by_strike: Dict[float, float], top_n: int = 5) -> Dict:
    """
    Краткая сводка GEX для передачи агенту.
    
    Returns:
        total_gex, max_positive_strike, max_negative_strike, top_positive, top_negative
    """
    if not gex_by_strike:
        return {
            'total_gex': 0.0,
            'max_positive_strike': None,
            'max_negative_strike': None,
            'top_positive_levels': [],
            'top_negative_levels': [],
        }
    total = total_gex(gex_by_strike)
    positive = [(s, v) for s, v in gex_by_strike.items() if v > 0]
    negative = [(s, v) for s, v in gex_by_strike.items() if v < 0]
    positive.sort(key=lambda x: -x[1])
    negative.sort(key=lambda x: x[1])
    return {
        'total_gex': total,
        'max_positive_strike': positive[0][0] if positive else None,
        'max_negative_strike': negative[0][0] if negative else None,
        'top_positive_levels': positive[:top_n],
        'top_negative_levels': negative[:top_n],
    }


def _round_level_to_nearest_strike(price: float, strikes: List[float]) -> Optional[float]:
    """Округлить значение уровня до ближайшего страйка. Возвращает None если strikes пустой."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - price))


def _strikes_centered_on_spot(
    strikes: List[float],
    underlying_price: Optional[float],
    max_strikes: int = 31
) -> List[float]:
    """
    Вернуть подсписок страйков так, чтобы ближайший к underlying_price страйк был в центре гистограммы.
    Если underlying_price None или strikes пустой — возвращаем все страйки без изменений.
    """
    if not strikes or underlying_price is None:
        return strikes
    nearest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_price))
    half = max_strikes // 2
    start = max(0, nearest_idx - half)
    end = min(len(strikes), start + max_strikes)
    if end - start < max_strikes and start > 0:
        start = max(0, end - max_strikes)
    return strikes[start:end]


def build_gex_chart_png(
    gex_by_strike: Dict[float, float],
    title: str = "GEX по страйкам",
    underlying_price: Optional[float] = None,
    support_resistance_levels: Optional[Dict[str, List[float]]] = None
) -> bytes:
    """
    Построить столбчатую диаграмму GEX по страйкам, вернуть PNG в виде bytes.
    
    Args:
        gex_by_strike: Словарь {strike: gex_value}
        title: Заголовок графика
        underlying_price: Цена базового актива (опционально — вертикальная линия на графике)
        support_resistance_levels: Уровни из БД {'support': [prices], 'resistance': [prices]};
            рисуются вертикальными линиями, округлёнными до ближайшего страйка.
    
    Returns:
        PNG изображение в байтах
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    if not gex_by_strike:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных для построения GEX', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    strikes = sorted(gex_by_strike.keys())
    strikes = _strikes_centered_on_spot(strikes, underlying_price)
    strike_to_idx = {s: i for i, s in enumerate(strikes)}
    values = [gex_by_strike[s] for s in strikes]
    colors = ['#2ecc71' if v >= 0 else '#e74c3c' for v in values]

    fig, ax = plt.subplots(figsize=(12, 6))
    bars = ax.bar(range(len(strikes)), values, color=colors, edgecolor='gray', linewidth=0.5)
    ax.axhline(y=0, color='black', linewidth=0.8)
    legend_handles = []
    if underlying_price is not None and strikes:
        idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_price))
        ax.axvline(x=idx, color='blue', linestyle='--', linewidth=1.5, alpha=0.8, label=f'Spot ~{underlying_price:,.0f}')
    if support_resistance_levels and strikes:
        support_strikes = set()
        for price in support_resistance_levels.get('support') or []:
            s = _round_level_to_nearest_strike(price, strikes)
            if s is not None:
                support_strikes.add(s)
        resistance_strikes = set()
        for price in support_resistance_levels.get('resistance') or []:
            s = _round_level_to_nearest_strike(price, strikes)
            if s is not None:
                resistance_strikes.add(s)
        for s in support_strikes:
            idx = strike_to_idx[s]
            ax.axvline(x=idx, color='#27ae60', linestyle='-', linewidth=1.2, alpha=0.9)
        for s in resistance_strikes:
            idx = strike_to_idx[s]
            ax.axvline(x=idx, color='#c0392b', linestyle='-', linewidth=1.2, alpha=0.9)
        if support_strikes:
            from matplotlib.lines import Line2D
            legend_handles.append(Line2D([0], [0], color='#27ae60', linewidth=2, label='Поддержка'))
        if resistance_strikes:
            from matplotlib.lines import Line2D
            legend_handles.append(Line2D([0], [0], color='#c0392b', linewidth=2, label='Сопротивление'))
    if underlying_price is not None and strikes:
        from matplotlib.lines import Line2D
        legend_handles.insert(0, Line2D([0], [0], color='blue', linestyle='--', linewidth=2, label=f'Spot ~{underlying_price:,.0f}'))
    if legend_handles:
        ax.legend(handles=legend_handles, loc='upper right', fontsize=8)
    ax.set_xticks(range(len(strikes)))
    ax.set_xticklabels([f'{s:,.0f}' for s in strikes], rotation=45, ha='right', fontsize=8)
    ax.set_ylabel('GEX')
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_oi_by_strike_chart_png(
    data_by_symbol: Dict[str, Dict],
    underlying: str,
    expiration_str: str,
    underlying_price: Optional[float] = None
) -> Optional[bytes]:
    """
    Гистограмма OI по страйкам: суммарный Open Interest для каждого страйка.
    Calls и Puts на одной диаграмме (группированные столбцы). Данные из доски (data_store).
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"
    oi_call: Dict[float, float] = defaultdict(float)
    oi_put: Dict[float, float] = defaultdict(float)
    for symbol, data in data_by_symbol.items():
        if not symbol.startswith(prefix):
            continue
        strike = _parse_symbol(symbol)[2]
        if strike is None:
            continue
        oi = data.get("open_interest")
        if oi is not None:
            try:
                oi = float(oi)
            except (TypeError, ValueError):
                continue
        else:
            continue
        if _is_call_symbol(symbol, data):
            oi_call[strike] += oi
        else:
            oi_put[strike] += oi

    strikes = sorted(set(oi_call.keys()) | set(oi_put.keys()))
    strikes = _strikes_centered_on_spot(strikes, underlying_price)
    if not strikes:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных для OI по страйкам', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    oi_calls = [oi_call.get(s, 0) for s in strikes]
    oi_puts = [oi_put.get(s, 0) for s in strikes]
    x = range(len(strikes))
    width = 0.35

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar([i - width / 2 for i in x], oi_calls, width, label='Calls', color='#3498db', edgecolor='gray', linewidth=0.5)
    ax.bar([i + width / 2 for i in x], oi_puts, width, label='Puts', color='#e74c3c', edgecolor='gray', linewidth=0.5)
    if underlying_price is not None and strikes:
        idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_price))
        ax.axvline(x=idx, color='gray', linestyle='--', linewidth=1.2, alpha=0.8, label=f'Spot ~{underlying_price:,.0f}')
    ax.set_xticks(x)
    ax.set_xticklabels([f'{s:,.0f}' for s in strikes], rotation=45, ha='right', fontsize=8)
    ax.set_ylabel('Open Interest')
    ax.set_title(f"OI по страйкам {underlying} {expiration_str}")
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def compute_iv_atm_from_board(
    data_by_symbol: Dict[str, Dict],
    underlying: str,
    expiration_str: str
) -> Optional[float]:
    """
    IV_ATM по текущей доске: (IV_PUT_OTM + IV_CALL_OTM) / 2.
    PUT OTM — ближайший страйк Put < underlying_price (max strike).
    CALL OTM — ближайший страйк Call > underlying_price (min strike).
    data_by_symbol: словарь из data_store.get_all() (symbol -> data с iv, underlying_price).
    """
    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"
    rows = []
    underlying_price = None
    for symbol, data in data_by_symbol.items():
        if not symbol.startswith(prefix):
            continue
        if underlying_price is None:
            underlying_price = data.get("underlying_price")
        strike = _parse_symbol(symbol)[2]
        opt_type = _option_type_from_symbol(symbol)
        iv = data.get("iv") or data.get("mark_iv") or data.get("ask_iv") or data.get("bid_iv")
        if strike is not None and opt_type and iv is not None:
            rows.append({
                "strike": strike,
                "option_type": opt_type,
                "iv": float(iv),
                "underlying_price": data.get("underlying_price"),
            })
    if not rows or underlying_price is None:
        return None
    try:
        underlying_price = float(underlying_price)
    except (TypeError, ValueError):
        return None
    put_otm = [r for r in rows if r["option_type"] == "P" and r["strike"] < underlying_price]
    call_otm = [r for r in rows if r["option_type"] == "C" and r["strike"] > underlying_price]
    if not put_otm or not call_otm:
        return None
    put_row = max(put_otm, key=lambda r: r["strike"])
    call_row = min(call_otm, key=lambda r: r["strike"])
    return (put_row["iv"] + call_row["iv"]) / 2.0


def _shorten_hour_label(hour_str: str) -> str:
    """Сократить метку времени для оси X: 'YYYY-MM-DD HH:00' -> 'DD.MM HH:00' (без года)."""
    if not hour_str or len(hour_str) < 16:
        return hour_str
    try:
        # "2026-02-28 12:00" -> "28.02 12:00"
        parts = hour_str.strip().split()
        if len(parts) >= 2:
            date_part = parts[0]  # YYYY-MM-DD
            time_part = parts[1][:5] if len(parts[1]) >= 5 else parts[1]  # HH:00
            y, m, d = date_part.split("-")
            return f"{int(d):02d}.{int(m):02d} {time_part}"
    except (ValueError, IndexError):
        pass
    return hour_str


def build_iv_chart_png(
    hourly_data: Sequence[Tuple[str, float]],
    title: str = "IV (ATM)",
    current_iv: Optional[float] = None
) -> bytes:
    """
    График IV по часам (ATM опционы). Возвращает PNG в виде bytes.
    
    Args:
        hourly_data: Список (hour_str, avg_iv)
        title: Заголовок графика
        current_iv: Текущее значение IV (выводится в подпись/заголовок)
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    if not hourly_data:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных IV за период', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    hours = [x[0] for x in hourly_data]
    values = [x[1] for x in hourly_data]
    hour_labels = [_shorten_hour_label(h) for h in hours]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(range(len(hours)), values, color='#3498db', marker='o', markersize=4, linewidth=1.5)
    ax.set_xticks(range(len(hours)))
    ax.set_xticklabels(hour_labels, rotation=45, ha='right', fontsize=8)
    ax.set_ylabel('IV (ATM)')
    ax.set_title(title + (f"  |  Текущее: {current_iv:.2%}" if current_iv is not None else ""))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_oi_chart_png(
    hourly_data: Sequence[Tuple[str, float]],
    title: str = "Открытый интерес",
    current_oi: Optional[float] = None
) -> bytes:
    """
    График открытого интереса по часам (все опционы экспирации). Возвращает PNG в виде bytes.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    if not hourly_data:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных OI за период', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    hours = [x[0] for x in hourly_data]
    values = [x[1] for x in hourly_data]
    hour_labels = [_shorten_hour_label(h) for h in hours]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(range(len(hours)), values, color='#9b59b6', marker='s', markersize=4, linewidth=1.5)
    ax.set_xticks(range(len(hours)))
    ax.set_xticklabels(hour_labels, rotation=45, ha='right', fontsize=8)
    ax.set_ylabel('Open Interest')
    ax.set_title(title + (f"  |  Текущее: {current_oi:,.0f}" if current_oi is not None else ""))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _smile_from_snapshot_rows(rows: List[Dict]) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Построить (calls, puts) для улыбки из снимка БД: rows с ключами strike, option_type, iv."""
    calls: List[Tuple[float, float]] = []
    puts: List[Tuple[float, float]] = []
    for r in rows:
        strike = r.get("strike")
        iv = r.get("iv")
        opt = (r.get("option_type") or "").upper()
        if strike is None or iv is None:
            continue
        try:
            s, v = float(strike), float(iv)
        except (TypeError, ValueError):
            continue
        if opt in ("C", "CALL"):
            calls.append((s, v))
        elif opt in ("P", "PUT"):
            puts.append((s, v))
    return (calls, puts)


def _is_call_symbol(symbol: str, data: Optional[Dict] = None) -> bool:
    """Определить, является ли опцион Call по символу или по data.option_type."""
    if data is not None:
        ot = (data.get("option_type") or "").upper()
        if ot in ("C", "CALL"):
            return True
        if ot in ("P", "PUT"):
            return False
    opt = _option_type_from_symbol(symbol)
    if opt == "C":
        return True
    if opt == "P":
        return False
    # Fallback: по конвенции Bybit в символе тип перед последней частью: ...-C-USDT или ...-P-USDT
    parts = symbol.split("-")
    if len(parts) >= 4:
        return (parts[3] or "").upper() == "C"
    return False


def build_volatility_smile_chart_png(
    data_by_symbol: Dict[str, Dict],
    underlying: str,
    expiration_str: str
) -> Optional[bytes]:
    """
    Точечный график «Улыбка волатильности»: X — страйки, Y — IV.
    Отдельные точки и линии для Calls и Puts. Вертикальная линия — underlying_price (округлён до ближайшего страйка).
    В шапке: высота улыбки (max(iv) - min(iv)) для Calls и Puts.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"
    # Поддержка варианта, когда underlying пришёл в другом регистре/раскладке
    prefix_alt = f"{underlying}-{expiration_str}-" if underlying != underlying.upper() or expiration_str != expiration_str.upper() else None
    calls = []  # (strike, iv)
    puts = []   # (strike, iv)
    underlying_price = None
    for symbol, data in data_by_symbol.items():
        if not symbol.startswith(prefix) and (not prefix_alt or not symbol.startswith(prefix_alt)):
            continue
        strike = _parse_symbol(symbol)[2]
        iv = data.get("iv") or data.get("mark_iv") or data.get("ask_iv") or data.get("bid_iv")
        if underlying_price is None and data.get("underlying_price"):
            try:
                underlying_price = float(data["underlying_price"])
            except (TypeError, ValueError):
                pass
        if strike is None or iv is None:
            continue
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if _is_call_symbol(symbol, data):
            calls.append((strike, iv))
        else:
            puts.append((strike, iv))

    if not calls and not puts:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных для улыбки волатильности', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    all_strikes_list = sorted(set(s for s, _ in calls) | set(s for s, _ in puts))
    strike_window = set(_strikes_centered_on_spot(all_strikes_list, underlying_price))
    calls = [(s, v) for s, v in calls if s in strike_window]
    puts = [(s, v) for s, v in puts if s in strike_window]

    height_calls = (max(iv for _, iv in calls) - min(iv for _, iv in calls)) if calls else 0.0
    height_puts = (max(iv for _, iv in puts) - min(iv for _, iv in puts)) if puts else 0.0
    title = f"Улыбка волатильности {underlying} {expiration_str} | Высота улыбки: Calls {height_calls:.2%}, Puts {height_puts:.2%}"

    fig, ax = plt.subplots(figsize=(12, 6))
    color_calls = "#3498db"
    color_puts = "#e74c3c"
    if calls:
        calls_sorted = sorted(calls, key=lambda x: x[0])
        strikes_c, ivs_c = zip(*calls_sorted)
        ax.plot(strikes_c, ivs_c, color=color_calls, linewidth=1, alpha=0.9)
        ax.scatter(strikes_c, ivs_c, color=color_calls, label="Calls", alpha=0.8, s=30)
    if puts:
        puts_sorted = sorted(puts, key=lambda x: x[0])
        strikes_p, ivs_p = zip(*puts_sorted)
        ax.plot(strikes_p, ivs_p, color=color_puts, linewidth=1, alpha=0.9)
        ax.scatter(strikes_p, ivs_p, color=color_puts, label="Puts", alpha=0.8, s=30)

    all_strikes = [s for s, _ in calls] + [s for s, _ in puts]
    if underlying_price is not None and all_strikes:
        nearest_strike = min(all_strikes, key=lambda s: abs(s - underlying_price))
        ax.axvline(x=nearest_strike, color="gray", linestyle="--", linewidth=1.2, alpha=0.8, label=f"Spot ~{underlying_price:,.0f}")

    ax.set_xlabel("Страйк")
    ax.set_ylabel("IV")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_volatility_smile_chart_png_three_series(
    data_by_symbol: Dict[str, Dict],
    underlying: str,
    expiration_str: str,
    snapshot_2h_ago: Optional[List[Dict]] = None,
    snapshot_yesterday: Optional[List[Dict]] = None,
    label_2h: str = "2 ч назад",
    label_yesterday: str = "Вчера 20:00",
) -> Optional[bytes]:
    """
    График «Улыбка волатильности» с тремя сериями: текущая, 2 ч назад, вчера 20:00.
    Серии разными цветами, в легенде подписи.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prefix = f"{underlying.upper()}-{expiration_str.upper()}-"
    prefix_alt = f"{underlying}-{expiration_str}-" if (underlying != underlying.upper() or expiration_str != expiration_str.upper()) else None
    calls_cur, puts_cur = [], []
    underlying_price = None
    for symbol, data in data_by_symbol.items():
        if not symbol.startswith(prefix) and (not prefix_alt or not symbol.startswith(prefix_alt)):
            continue
        strike = _parse_symbol(symbol)[2]
        iv = data.get("iv") or data.get("mark_iv") or data.get("ask_iv") or data.get("bid_iv")
        if underlying_price is None and data.get("underlying_price"):
            try:
                underlying_price = float(data["underlying_price"])
            except (TypeError, ValueError):
                pass
        if strike is None or iv is None:
            continue
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if _is_call_symbol(symbol, data):
            calls_cur.append((strike, iv))
        else:
            puts_cur.append((strike, iv))

    calls_2h, puts_2h = _smile_from_snapshot_rows(snapshot_2h_ago) if snapshot_2h_ago else ([], [])
    calls_yest, puts_yest = _smile_from_snapshot_rows(snapshot_yesterday) if snapshot_yesterday else ([], [])

    all_strikes_set = set()
    for c, p in [(calls_cur, puts_cur), (calls_2h, puts_2h), (calls_yest, puts_yest)]:
        for s, _ in c:
            all_strikes_set.add(s)
        for s, _ in p:
            all_strikes_set.add(s)
    all_strikes_list = sorted(all_strikes_set)
    if not all_strikes_list:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных для улыбки волатильности', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    strike_window = set(_strikes_centered_on_spot(all_strikes_list, underlying_price))

    def filter_series(calls: List[Tuple[float, float]], puts: List[Tuple[float, float]]):
        return (
            [(s, v) for s, v in calls if s in strike_window],
            [(s, v) for s, v in puts if s in strike_window],
        )

    calls_cur, puts_cur = filter_series(calls_cur, puts_cur)
    calls_2h, puts_2h = filter_series(calls_2h, puts_2h)
    calls_yest, puts_yest = filter_series(calls_yest, puts_yest)

    if not calls_cur and not puts_cur:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.text(0.5, 0.5, 'Нет данных для улыбки волатильности', ha='center', va='center', fontsize=14)
        ax.axis('off')
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    height_c = (max(iv for _, iv in calls_cur) - min(iv for _, iv in calls_cur)) if calls_cur else 0.0
    height_p = (max(iv for _, iv in puts_cur) - min(iv for _, iv in puts_cur)) if puts_cur else 0.0
    title = f"Улыбка волатильности {underlying} {expiration_str} | Высота: C {height_c:.2%}, P {height_p:.2%}"

    fig, ax = plt.subplots(figsize=(12, 6))

    series = [
        (calls_cur, puts_cur, "Текущая", "#3498db", "#e74c3c"),
        (calls_2h, puts_2h, label_2h, "#27ae60", "#e67e22"),
        (calls_yest, puts_yest, label_yesterday, "#9b59b6", "#1abc9c"),
    ]
    for calls, puts, label, color_c, color_p in series:
        if calls:
            cs = sorted(calls, key=lambda x: x[0])
            strikes_c, ivs_c = zip(*cs)
            ax.plot(strikes_c, ivs_c, color=color_c, linewidth=1.2, alpha=0.9)
            ax.scatter(strikes_c, ivs_c, color=color_c, label=f"{label} Calls", alpha=0.8, s=24)
        if puts:
            ps = sorted(puts, key=lambda x: x[0])
            strikes_p, ivs_p = zip(*ps)
            ax.plot(strikes_p, ivs_p, color=color_p, linewidth=1.2, alpha=0.9)
            ax.scatter(strikes_p, ivs_p, color=color_p, label=f"{label} Puts", alpha=0.8, s=24)

    if underlying_price is not None and strike_window:
        nearest = min(strike_window, key=lambda s: abs(s - underlying_price))
        ax.axvline(x=nearest, color="gray", linestyle="--", linewidth=1.2, alpha=0.8, label=f"Spot ~{underlying_price:,.0f}")

    ax.set_xlabel("Страйк")
    ax.set_ylabel("IV")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
