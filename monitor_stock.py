#!/usr/bin/env python3
"""
Моніторинг наявності розміру S для тренча Reserved 493IA-80X.

Локально:
  python3 monitor_stock.py
  python3 monitor_stock.py --watch
  python3 monitor_stock.py --test-telegram

У хмарі (GitHub Actions) ноут може бути вимкнений — див. workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

SKU = "493IA-80X"
STORE_ID = "1007"  # Reserved UA
TARGET_SIZE = "S"
API_URL = f"https://arch.reserved.com/api/{STORE_ID}/product/{SKU}"
PRODUCT_URL = "https://www.reserved.com/ua/uk/trench-z-bavovnoiu-493ia-80x"
TZ = ZoneInfo("Europe/Kyiv")

BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "availability_log.jsonl"
STATE_PATH = BASE_DIR / "last_state.json"


def now_kyiv() -> datetime:
    return datetime.now(TZ)


def fetch_product() -> dict:
    req = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Origin": "https://www.reserved.com",
            "Referer": PRODUCT_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def size_status(product: dict, size_letter: str = TARGET_SIZE) -> dict | None:
    for size in product.get("sizes") or []:
        name = (size.get("sizeName") or "").upper()
        sku = (size.get("sku") or "").upper()
        if name.startswith(f"{size_letter} ") or sku.endswith(f"-{size_letter}"):
            return {
                "size_name": size.get("sizeName"),
                "sku": size.get("sku"),
                "in_stock": bool(size.get("isInStock") or size.get("stock")),
                "stock_quantity": size.get("stockQuantity"),
                "in_transit": bool(
                    size.get("isStockInTransit") or size.get("inTransitStock")
                ),
            }
    return None


def all_sizes_summary(product: dict) -> list[dict]:
    rows = []
    for size in product.get("sizes") or []:
        rows.append(
            {
                "size_name": size.get("sizeName"),
                "in_stock": bool(size.get("isInStock") or size.get("stock")),
                "stock_quantity": size.get("stockQuantity"),
            }
        )
    return rows


def append_log(entry: dict) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_last_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def notify_macos(title: str, message: str) -> None:
    try:
        import subprocess

        script = (
            f'display notification {json.dumps(message)} '
            f'with title {json.dumps(title)}'
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass


def notify_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    print("Telegram: повідомлення надіслано")
    return True


def notify_email(subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    mail_from = os.environ.get("EMAIL_FROM", user).strip()
    mail_to = os.environ.get("EMAIL_TO", "").strip()
    if not all([host, user, password, mail_from, mail_to]):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"Email: надіслано на {mail_to}")
    return True


def send_alerts(title: str, body: str, *, local_macos: bool) -> None:
    sent = False
    try:
        sent = notify_telegram(f"{title}\n\n{body}") or sent
    except Exception as exc:
        print(f"Помилка Telegram: {exc}", file=sys.stderr)

    try:
        sent = notify_email(title, body) or sent
    except Exception as exc:
        print(f"Помилка email: {exc}", file=sys.stderr)

    if local_macos:
        notify_macos(title, body.replace("\n", " "))

    if not sent and not local_macos:
        print(
            "Сповіщення не надіслано: задайте TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
            "або SMTP_* / EMAIL_*",
            file=sys.stderr,
        )


def appearance_message(product: dict, status: dict, entry: dict) -> tuple[str, str]:
    title = "Reserved: розмір S з’явився в наявності"
    qty = status.get("stock_quantity")
    qty_line = f"Кількість: {qty}\n" if qty not in (None, 0) else ""
    body = (
        f"{product.get('name')}\n"
        f"Розмір: {status.get('size_name')}\n"
        f"Ціна: {entry.get('price')} {entry.get('currency')}\n"
        f"Коли: {entry.get('day')} {entry.get('hour')} (Київ)\n"
        f"{qty_line}"
        f"{PRODUCT_URL}"
    )
    return title, body


def check_once(*, local_macos: bool = True, cloud: bool = False) -> dict:
    checked_at = now_kyiv()
    product = fetch_product()
    status = size_status(product)
    if status is None:
        raise RuntimeError(f"Розмір {TARGET_SIZE} не знайдено у відповіді API")

    entry = {
        "checked_at": checked_at.isoformat(timespec="seconds"),
        "day": checked_at.strftime("%Y-%m-%d"),
        "hour": checked_at.strftime("%H:%M"),
        "weekday": checked_at.strftime("%A"),
        "product_name": product.get("name"),
        "product_sku": product.get("sku"),
        "price": product.get("finalPrice"),
        "currency": product.get("currency"),
        "size": status,
        "all_sizes": all_sizes_summary(product),
        "product_url": PRODUCT_URL,
    }
    if not cloud:
        append_log(entry)

    prev = load_last_state()
    prev_in_stock = None if prev is None else bool(prev.get("in_stock"))
    now_in_stock = status["in_stock"]

    # None -> True теж рахуємо появою (перший раз побачили в наявності)
    appeared = now_in_stock and prev_in_stock is not True
    disappeared = prev_in_stock is True and now_in_stock is False

    save_state(
        {
            "checked_at": entry["checked_at"],
            "in_stock": now_in_stock,
            "stock_quantity": status["stock_quantity"],
            "size_name": status["size_name"],
        }
    )

    if appeared:
        entry["event"] = "appeared"
        print(
            f"З’ЯВИЛОСЬ У НАЯВНОСТІ: {status['size_name']} "
            f"о {entry['day']} {entry['hour']} (Київ)"
        )
        print(f"   {PRODUCT_URL}")
        title, body = appearance_message(product, status, entry)
        send_alerts(title, body, local_macos=local_macos and not cloud)
    elif disappeared:
        entry["event"] = "disappeared"
        print(
            f"Зникло з наявності: {status['size_name']} "
            f"о {entry['day']} {entry['hour']} (Київ)"
        )
    else:
        mark = "є" if now_in_stock else "немає"
        qty = status["stock_quantity"]
        qty_txt = f", qty={qty}" if qty is not None else ""
        print(
            f"[{entry['day']} {entry['hour']}] розмір {TARGET_SIZE}: "
            f"{mark} ({status['size_name']}{qty_txt})"
        )

    if cloud:
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write(f"in_stock={str(now_in_stock).lower()}\n")
                f.write(f"appeared={str(appeared).lower()}\n")

    return entry


def report() -> None:
    if not LOG_PATH.exists():
        print("Лог ще порожній. Спочатку запустіть моніторинг.")
        return

    available_hours: list[tuple[str, str]] = []
    appeared_events: list[str] = []
    last = None

    with LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            in_stock = bool(entry.get("size", {}).get("in_stock"))
            stamp = f"{entry.get('day')} {entry.get('hour')}"
            if in_stock:
                available_hours.append((entry.get("day", ""), entry.get("hour", "")))
            if last is not None and (not last) and in_stock:
                appeared_events.append(stamp)
            last = in_stock

    print(f"Файл логу: {LOG_PATH}")
    print(f"Перевірок з наявністю S: {len(available_hours)}")
    if appeared_events:
        print("Моменти появи в наявності (перехід немає → є):")
        for stamp in appeared_events:
            print(f"  • {stamp}")
    elif available_hours:
        print("Години, коли під час перевірки розмір S був у наявності:")
        for day, hour in available_hours:
            print(f"  • {day} {hour}")
    else:
        print("За весь час логу розмір S у наявності не фіксувався.")


def watch(every_minutes: float, local_macos: bool) -> None:
    print(
        f"Моніторинг {SKU}, розмір {TARGET_SIZE}. "
        f"Інтервал: {every_minutes} хв. Ctrl+C щоб зупинити."
    )
    print(f"Лог: {LOG_PATH}")
    while True:
        try:
            check_once(local_macos=local_macos, cloud=False)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            stamp = now_kyiv().strftime("%Y-%m-%d %H:%M")
            print(f"[{stamp}] помилка мережі: {exc}", file=sys.stderr)
        except Exception as exc:
            stamp = now_kyiv().strftime("%Y-%m-%d %H:%M")
            print(f"[{stamp}] помилка: {exc}", file=sys.stderr)
        time.sleep(max(every_minutes, 1) * 60)


def test_telegram() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "Задайте змінні оточення:\n"
            "  export TELEGRAM_BOT_TOKEN='...'\n"
            "  export TELEGRAM_CHAT_ID='...'",
            file=sys.stderr,
        )
        return 1
    notify_telegram(
        "Тест моніторингу Reserved\n\n"
        "Якщо бачите це — Telegram підключено правильно."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Моніторинг наявності розміру S на Reserved"
    )
    parser.add_argument("--watch", action="store_true", help="перевіряти постійно")
    parser.add_argument(
        "--every",
        type=float,
        default=10,
        help="інтервал у хвилинах для --watch (за замовчуванням 10)",
    )
    parser.add_argument("--report", action="store_true", help="звіт з локального логу")
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="режим GitHub Actions: Telegram/email, без macOS",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="надіслати тестове повідомлення в Telegram",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="не показувати локальні macOS-сповіщення",
    )
    args = parser.parse_args()

    if args.test_telegram:
        return test_telegram()

    if args.report:
        report()
        return 0

    if args.watch:
        watch(args.every, local_macos=not args.no_notify)
        return 0

    check_once(local_macos=not args.no_notify and not args.cloud, cloud=args.cloud)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
        raise SystemExit(0)
