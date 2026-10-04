"""
Бот + витрина (Telegram Mini App) для продажи фото за Stars.
pip install aiogram>=3.13 aiohttp pillow
Запуск: BOT_TOKEN=... ADMIN_ID=... WEBAPP_URL=https://твой-домен python bot.py
Файлы рядом: index.html (витрина). Папка media/ создастся сама.
"""
import asyncio, hashlib, hmac, json, os, sqlite3
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (FSInputFile, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb,
                           LabeledPrice, Message, PreCheckoutQuery, WebAppInfo)
from PIL import Image, ImageFilter

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")  # обязательно https
MEDIA = Path("media"); MEDIA.mkdir(exist_ok=True)

bot = Bot(TOKEN)
dp = Dispatcher()
db = sqlite3.connect("shop.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, title, price INT);
CREATE TABLE IF NOT EXISTS purchases (user_id INT, item_id INT, charge_id);""")


# ================= БОТ =================
@dp.message(CommandStart())
async def start(m: Message):
    kb = Kb(inline_keyboard=[[Btn(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Фото в полном качестве. Оплата звёздами ⭐ прямо в магазине.", reply_markup=kb)


# Админ: фото (лучше как файл, без сжатия) с подписью "Название | цена"
@dp.message(F.photo | F.document, F.from_user.id == ADMIN_ID)
async def add_item(m: Message):
    try:
        title, price = [x.strip() for x in (m.caption or "").split("|")]
        price = int(price)
    except ValueError:
        return await m.answer('Подпись: "Название | цена в ⭐"')
    i = db.execute("INSERT INTO items (title, price) VALUES (?,?)", (title, price)).lastrowid
    db.commit()
    await bot.download(m.photo[-1] if m.photo else m.document, MEDIA / f"{i}.jpg")  # оригинал (не публичный)
    im = Image.open(MEDIA / f"{i}.jpg").convert("RGB")
    im.thumbnail((700, 700))
    im.filter(ImageFilter.GaussianBlur(12)).save(MEDIA / f"{i}_p.jpg", quality=70)  # размытое превью
    await m.answer(f"Добавлено #{i}: {title}, {price} ⭐\nСсылка на товар: /link {i}")


# Прямая ссылка на товар (нужен Mini App с коротким именем shop, см. /newapp в BotFather)
@dp.message(Command("link"), F.from_user.id == ADMIN_ID)
async def link(m: Message, command: CommandObject):
    me = await bot.me()
    await m.answer(f"https://t.me/{me.username}/shop?startapp=item_{command.args}")


# Подтверждение оплаты (ответить нужно за 10 секунд)
@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


# Оплата прошла: сохраняем и отправляем оригинал файлом
@dp.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    i = int(p.invoice_payload.split(":")[0])
    db.execute("INSERT INTO purchases VALUES (?,?,?)", (m.from_user.id, i, p.telegram_payment_charge_id))
    db.commit()
    title = db.execute("SELECT title FROM items WHERE id=?", (i,)).fetchone()[0]
    await m.answer_document(FSInputFile(MEDIA / f"{i}.jpg", filename=f"{title}.jpg"),
                            caption=f"Спасибо за покупку! «{title}»")


@dp.message(Command("refund"), F.from_user.id == ADMIN_ID)
async def refund(m: Message, command: CommandObject):
    row = db.execute("SELECT user_id FROM purchases WHERE charge_id=?", (command.args,)).fetchone()
    if not row:
        return await m.answer("Платёж не найден")
    await bot.refund_star_payment(row[0], command.args)
    await m.answer("Звёзды возвращены")


@dp.message(Command("paysupport"))  # Telegram требует эту команду у платных ботов
async def paysupport(m: Message):
    await m.answer("По вопросам оплаты и возвратов пишите: @your_username")


# ================= ВЕБ-СЕРВЕР ВИТРИНЫ =================
def check_init_data(init_data: str) -> dict:
    """Проверка подписи Telegram: убеждаемся, что запрос пришёл из настоящего Mini App."""
    data = dict(parse_qsl(init_data, strict_parsing=True))
    got = data.pop("hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        raise ValueError("bad hash")
    return json.loads(data["user"])


async def products(_):
    rows = db.execute("SELECT id, title, price FROM items ORDER BY id DESC").fetchall()
    return web.json_response([{"id": i, "title": t, "price": p, "img": f"/p/{i}"} for i, t, p in rows])


async def invoice(req: web.Request):
    d = await req.json()
    try:
        user = check_init_data(d["init_data"])
        item_id = int(d["item_id"])
    except Exception:
        return web.json_response({"error": "Откройте магазин из Telegram"}, status=403)
    row = db.execute("SELECT title, price FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return web.json_response({"error": "Товар не найден"}, status=404)
    link = await bot.create_invoice_link(
        title=row[0], description="Фотография в полном качестве",
        payload=f"{item_id}:{user['id']}", currency="XTR",  # XTR = Stars, provider_token не нужен
        prices=[LabeledPrice(label=row[0], amount=row[1])])
    return web.json_response({"link": link})


async def preview(req: web.Request):  # отдаём ТОЛЬКО размытые превью, оригиналы наружу не торчат
    f = MEDIA / f"{req.match_info['id']}_p.jpg"
    return web.FileResponse(f) if f.exists() else web.Response(status=404)


async def main():
    app = web.Application()
    app.add_routes([
        web.get("/", lambda _: web.FileResponse("index.html")),
        web.get("/api/products", products),
        web.post("/api/invoice", invoice),
        web.get(r"/p/{id:\d+}", preview),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
