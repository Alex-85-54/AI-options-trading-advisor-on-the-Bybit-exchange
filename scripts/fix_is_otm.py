#!/usr/bin/env python3
"""
Скрипт миграции: перерасчёт is_otm и заполнение option_type, strike в option_history.

- is_otm: 1 = OTM, 0 = ITM (по логике Call/Put и underlying_price vs strike).
- option_type, strike: парсятся из тикера (например BTC-4JAN26-89000-C-USDT).

Логика is_otm:
  CALL (C): underlying_price < strike → OTM(1); underlying_price >= strike → ITM(0).
  PUT (P):  underlying_price <= strike → ITM(0); underlying_price > strike → OTM(1).

При первом запуске при необходимости добавляются колонки option_type (TEXT) и strike (REAL).

Запуск:
  uv run scripts/fix_is_otm.py
  uv run scripts/fix_is_otm.py --dry-run
  python scripts/fix_is_otm.py
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# Корень проекта (родитель каталога scripts)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "options.db"


def is_otm(underlying_price: float, strike: float, option_type: str) -> int:
    """
    Определить, является ли опцион OTM (1) или ITM (0).
    option_type: 'C' или 'P' (Call / Put).
    """
    option_type = (option_type or "C").upper()
    if option_type == "C":
        return 1 if underlying_price < strike else 0
    elif option_type == "P":
        return 1 if underlying_price > strike else 0
    return 1  # по умолчанию OTM при неизвестном типе


def parse_symbol(symbol: str):
    """Извлечь strike и option_type из символа (например BTC-4JAN26-89000-C-USDT)."""
    parts = symbol.split("-")
    if len(parts) < 4:
        return None, None
    try:
        strike = float(parts[2])
        option_type = parts[3].upper()
        return strike, option_type
    except (ValueError, IndexError):
        return None, None


def ensure_columns(cursor: sqlite3.Cursor) -> None:
    """Добавить колонки option_type и strike в option_history, если их нет."""
    cursor.execute("PRAGMA table_info(option_history)")
    columns = [row[1] for row in cursor.fetchall()]
    if "option_type" not in columns:
        cursor.execute("ALTER TABLE option_history ADD COLUMN option_type TEXT")
        print("Добавлена колонка option_type")
    if "strike" not in columns:
        cursor.execute("ALTER TABLE option_history ADD COLUMN strike REAL")
        print("Добавлена колонка strike")


def main() -> int:
    parser = argparse.ArgumentParser(description="Перерасчёт is_otm и заполнение option_type, strike в option_history")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не записывать в БД, только показать статистику",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Ошибка: база данных не найдена: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if not args.dry_run:
        ensure_columns(cursor)
        conn.commit()

    try:
        cursor.execute(
            "SELECT id, symbol, underlying_price FROM option_history"
        )
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Ошибка чтения option_history: {e}", file=sys.stderr)
        conn.close()
        return 1

    updated = 0
    set_otm = 0
    set_itm = 0
    skipped = 0

    for row in rows:
        symbol = row["symbol"]
        underlying_price = row["underlying_price"]
        if underlying_price is None:
            skipped += 1
            continue
        strike, option_type = parse_symbol(symbol)
        if strike is None:
            skipped += 1
            continue
        new_is_otm = is_otm(underlying_price, strike, option_type)
        if not args.dry_run:
            cursor.execute(
                "UPDATE option_history SET is_otm = ?, option_type = ?, strike = ? WHERE id = ?",
                (new_is_otm, option_type, strike, row["id"]),
            )
        updated += 1
        if new_is_otm == 1:
            set_otm += 1
        else:
            set_itm += 1

    if not args.dry_run:
        try:
            conn.commit()
        except sqlite3.Error as e:
            print(f"Ошибка коммита: {e}", file=sys.stderr)
            conn.rollback()
            conn.close()
            return 1
    else:
        print("Режим --dry-run: изменения в БД не вносились.")

    conn.close()

    print(f"Готово. Обработано записей: {updated}")
    print(f"  is_otm=1 (OTM): {set_otm}")
    print(f"  is_otm=0 (ITM): {set_itm}")
    if skipped:
        print(f"  Пропущено (нет цены или неверный символ): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
