from typing import Dict, List, Tuple
import pandas as pd
from datetime import datetime


def calculate_price_differences(options_data: Dict) -> Dict:
    """Рассчитать разницы цен между Call и Put опционами"""
    results = []

    # Группируем по underlying и strike
    grouped = {}
    for symbol, data in options_data.items():
        if 'ask_price' not in data or data['ask_price'] <= 0:
            continue

        # Парсим символ
        parts = symbol.split('-')
        if len(parts) >= 4:
            underlying = parts[0]
            strike = parts[2]
            option_type = parts[3]

            key = f"{underlying}_{strike}"
            if key not in grouped:
                grouped[key] = {'call': None, 'put': None}

            if 'C' in option_type:
                grouped[key]['call'] = {
                    'symbol': symbol,
                    'price': data['ask_price'],
                    'data': data
                }
            elif 'P' in option_type:
                grouped[key]['put'] = {
                    'symbol': symbol,
                    'price': data['ask_price'],
                    'data': data
                }

    # Рассчитываем разницы
    for key, pair in grouped.items():
        if pair['call'] and pair['put']:
            call_price = pair['call']['price']
            put_price = pair['put']['price']
            price_diff = abs(call_price - put_price)
            avg_price = (call_price + put_price) / 2

            if avg_price > 0:
                percent_diff = (price_diff / avg_price) * 100

                results.append({
                    'pair_key': key,
                    'call_symbol': pair['call']['symbol'],
                    'put_symbol': pair['put']['symbol'],
                    'call_price': call_price,
                    'put_price': put_price,
                    'price_difference': price_diff,
                    'percent_difference': percent_diff,
                    'avg_price': avg_price,
                    'timestamp': datetime.now()
                })

    # Сортируем по разнице в процентах (от меньшей к большей)
    results.sort(key=lambda x: x['percent_difference'])

    return {
        'total_pairs': len(results),
        'pairs': results[:10],  # Топ-10 пар с наименьшей разницей
        'min_difference': min([r['percent_difference'] for r in results]) if results else None,
        'max_difference': max([r['percent_difference'] for r in results]) if results else None,
        'avg_difference': sum([r['percent_difference'] for r in results]) / len(results) if results else None
    }


def format_price_message(pair_data: Dict) -> str:
    """Форматировать сообщение о разнице цен"""
    emoji = "🚨" if pair_data['percent_difference'] < 1.0 else "⚠️" if pair_data['percent_difference'] < 3.0 else "ℹ️"

    message = (
        f"{emoji} *{pair_data['pair_key']}*\n"
        f"Call: `{pair_data['call_symbol']}`\n"
        f"Цена: {pair_data['call_price']:.2f}\n"
        f"Put: `{pair_data['put_symbol']}`\n"
        f"Цена: {pair_data['put_price']:.2f}\n"
        f"Разница: {pair_data['price_difference']:.4f}\n"
        f"Проценты: {pair_data['percent_difference']:.2f}%\n"
        f"Время: {pair_data['timestamp'].strftime('%H:%M:%S')}"
    )

    return message