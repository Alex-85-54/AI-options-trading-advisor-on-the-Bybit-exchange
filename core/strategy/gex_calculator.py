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
GEX_MAX_DTE = 3


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
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


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
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(range(len(hours)), values, color='#3498db', marker='o', markersize=4, linewidth=1.5)
    ax.set_xticks(range(len(hours)))
    ax.set_xticklabels(hours, rotation=45, ha='right', fontsize=8)
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
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(range(len(hours)), values, color='#9b59b6', marker='s', markersize=4, linewidth=1.5)
    ax.set_xticks(range(len(hours)))
    ax.set_xticklabels(hours, rotation=45, ha='right', fontsize=8)
    ax.set_ylabel('Open Interest')
    ax.set_title(title + (f"  |  Текущее: {current_oi:,.0f}" if current_oi is not None else ""))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()
