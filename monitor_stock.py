#!/usr/bin/env python3
"""
Хмарний/локальний моніторинг наявності кількох товарів (Reserved + Zara).

  python3 monitor_stock.py
  python3 monitor_stock.py --cloud
  python3 monitor_stock.py --test-telegram
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

TZ = ZoneInfo("Europe/Kyiv")
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_PATH = BASE_DIR / "products.json"
STATE_PATH = BASE_DIR / "state.json"
LEGACY_STATE_PATH = BASE_DIR / "last_state.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def now_kyiv() -> datetime:
    return datetime.now(TZ)


def http_json(url: str, *, referer: str | None = None) -> dict | list:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.split("/ua/")[0] if "/ua/" in referer else referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_products() -> list[dict]:
    data = json.loads(PRODUCTS_PATH.read_text(encoding="utf-8"))
    return list(data.get("products") or [])


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Міграція зі старого single-product стейту
    if LEGACY_STATE_PATH.exists():
        try:
            legacy = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
            return {
                "reserved-trench-493IA-80X": {
                    "sizes": {
                        "S": {
                            "in_stock": bool(legacy.get("in_stock")),
                            "checked_at": legacy.get("checked_at"),
                        }
                    }
                }
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def send_alerts(title: str, body: str, *, local_macos: bool) -> None:
    text = f"{title}\n\n{body}"
    sent = False
    try:
        sent = notify_telegram(text) or sent
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
            "Сповіщення не надіслано: задайте TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID",
            file=sys.stderr,
        )


def format_alert(product: dict, sizes: list[str], price: str | None = None) -> tuple[str, str]:
    when = now_kyiv().strftime("%Y-%m-%d %H:%M")
    sizes_txt = ", ".join(sizes)
    mapping = {
        "sizes": sizes_txt,
        "when": f"{when} (Київ)",
        "url": product.get("url", ""),
        "price": price or "—",
        "name": product.get("name", ""),
    }
    title = product.get("alert_title") or f"{product.get('name')}: {sizes_txt} в наявності!"
    body = product.get("alert_body") or "{name}\nРозмір: {sizes}\n{when}\n{url}"
    for key, value in mapping.items():
        title = title.replace("{" + key + "}", str(value))
        body = body.replace("{" + key + "}", str(value))
    return title, body


def check_reserved(product: dict) -> dict[str, dict]:
    sku = product["sku"]
    store_id = product.get("store_id", "1007")
    url = f"https://arch.reserved.com/api/{store_id}/product/{sku}"
    data = http_json(url, referer=product.get("url"))
    wanted = {s.upper() for s in product.get("sizes") or []}
    result: dict[str, dict] = {}
    price = None
    if data.get("finalPrice") is not None:
        price = f"{data.get('finalPrice')} {data.get('currency') or 'UAH'}"

    for size in data.get("sizes") or []:
        name = (size.get("sizeName") or "").upper()
        size_sku = (size.get("sku") or "").upper()
        letter = None
        for wanted_size in wanted:
            if name.startswith(f"{wanted_size} ") or size_sku.endswith(f"-{wanted_size}"):
                letter = wanted_size
                break
        if not letter:
            continue
        # Онлайн на сайті: можна додати в кошик і оформити замовлення
        in_stock = bool(size.get("isInStock") or size.get("stock")) and not bool(
            data.get("isBlockedToCart")
        )
        result[letter] = {
            "in_stock": in_stock,
            "label": size.get("sizeName") or letter,
            "quantity": size.get("stockQuantity"),
            "price": price,
        }
    return result


def check_zara(product: dict) -> dict[str, dict]:
    product_id = product["product_id"]
    store_id = product.get("zara_store_id", "10701")
    url = (
        f"https://www.zara.com/itxrest/2/catalog/store/{store_id}/"
        f"product/id/{product_id}/availability"
    )
    data = http_json(url, referer="https://www.zara.com/ua/uk/")
    sku_map = {
        str(k).upper(): int(v)
        for k, v in (product.get("size_skus") or {}).items()
    }
    availability = {
        int(item["sku"]): str(item.get("availability") or "").lower()
        for item in data.get("skusAvailability") or []
    }
    # Тільки статуси, з якими на сайті зазвичай можна замовити онлайн
    in_stock_values = {"in_stock", "low_on_stock"}
    result: dict[str, dict] = {}
    for size_letter in product.get("sizes") or []:
        key = size_letter.upper()
        sku = sku_map.get(key)
        if sku is None:
            print(
                f"  ! для {product['id']} немає size_skus.{key} у products.json",
                file=sys.stderr,
            )
            continue
        status = availability.get(sku, "unknown")
        result[key] = {
            "in_stock": status in in_stock_values,
            "label": key,
            "quantity": None,
            "price": None,
            "raw_status": status,
            "sku": sku,
        }
    return result


def check_product(product: dict) -> dict[str, dict]:
    shop = (product.get("shop") or "").lower()
    if shop == "reserved":
        return check_reserved(product)
    if shop == "zara":
        return check_zara(product)
    raise ValueError(f"Невідомий shop: {shop}")


def process_product(
    product: dict,
    state: dict,
    *,
    local_macos: bool,
) -> None:
    pid = product["id"]
    checked_at = now_kyiv().isoformat(timespec="seconds")
    print(f"\n== {product.get('name')} ({pid})")
    try:
        sizes_status = check_product(product)
    except Exception as exc:
        print(f"  помилка перевірки: {exc}", file=sys.stderr)
        return

    prev_product = state.get(pid) or {}
    prev_sizes = prev_product.get("sizes") or {}
    new_sizes_state: dict[str, dict] = {}
    appeared: list[str] = []
    price = None

    for size_letter, info in sizes_status.items():
        in_stock = bool(info.get("in_stock"))
        prev = prev_sizes.get(size_letter)
        prev_in_stock = None if prev is None else bool(prev.get("in_stock"))
        mark = "є" if in_stock else "немає"
        extra = ""
        if info.get("raw_status"):
            extra = f", status={info['raw_status']}"
        if info.get("quantity") is not None:
            extra += f", qty={info['quantity']}"
        print(f"  [{now_kyiv():%Y-%m-%d %H:%M}] {size_letter}: {mark}{extra}")

        if in_stock and prev_in_stock is not True:
            appeared.append(size_letter)
        if info.get("price"):
            price = info["price"]

        new_sizes_state[size_letter] = {
            "in_stock": in_stock,
            "checked_at": checked_at,
            "label": info.get("label"),
            "raw_status": info.get("raw_status"),
            "sku": info.get("sku"),
        }

    state[pid] = {"checked_at": checked_at, "sizes": new_sizes_state}

    if appeared:
        title, body = format_alert(product, appeared, price=price)
        print(f"  ALERT: {title}")
        send_alerts(title, body, local_macos=local_macos)


def run_once(*, cloud: bool, local_macos: bool) -> int:
    products = load_products()
    state = load_state()
    for product in products:
        process_product(product, state, local_macos=local_macos and not cloud)
    save_state(state)
    return 0


def watch(every_minutes: float, local_macos: bool) -> None:
    print(f"Моніторинг кожні {every_minutes} хв. Ctrl+C щоб зупинити.")
    while True:
        try:
            run_once(cloud=False, local_macos=local_macos)
        except Exception as exc:
            print(f"помилка: {exc}", file=sys.stderr)
        time.sleep(max(every_minutes, 1) * 60)


def test_telegram() -> int:
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("Задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1
    notify_telegram(
        "Тест моніторингу\n\nReserved + Zara підключені. Якщо бачите це — все ок."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Моніторинг Reserved + Zara")
    parser.add_argument("--cloud", action="store_true", help="режим GitHub Actions")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--every", type=float, default=10)
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    if args.test_telegram:
        return test_telegram()
    if args.watch:
        watch(args.every, local_macos=not args.no_notify)
        return 0
    return run_once(cloud=args.cloud, local_macos=not args.no_notify and not args.cloud)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nЗупинено.")
        raise SystemExit(0)
