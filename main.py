import os, hmac, hashlib, json, time, asyncio, html
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (Update, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
                           BufferedInputFile, BotCommand, MenuButtonWebApp)
from fastapi import FastAPI, Depends, Header, HTTPException, Request, BackgroundTasks, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from models import Base, User, Product, Favorite, Order, Promo, Setting, Review

E = os.environ
BOT_TOKEN = E["BOT_TOKEN"]
ADMINS = {int(x) for x in E.get("ADMIN_ID", "").split(",") if x.strip()}   # asosiy adminlar (env)
DYN: set[int] = set()                                                      # panel orqali qo‘shilgan adminlar
URL = (E.get("TELEGRAM_WEBAPP_URL") or E.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
SECRET = E.get("SECRET_KEY", "change-me")
DBURL = E["DATABASE_URL"].split("?")[0].replace("postgres://", "postgresql://").replace("postgresql://", "postgresql+asyncpg://")

is_admin = lambda uid: uid in ADMINS or uid in DYN
admin_ids = lambda: ADMINS | DYN

ST = {"new": "🟡 Buyurtmangiz qabul qilindi", "confirmed": "🔵 Buyurtmangiz tasdiqlandi", "preparing": "🟠 Buyurtmangiz tayyorlanmoqda",
      "shipping": "🚚 Buyurtmangiz kuryerga berildi", "delivered": "🟢 Buyurtmangiz yetkazildi", "cancelled": "🔴 Buyurtmangiz bekor qilindi"}
SL = {"new": "🟡 Yangi", "confirmed": "🔵 Tasdiqlangan", "preparing": "🟠 Tayyorlanmoqda", "shipping": "🚚 Yetkazilmoqda",
      "delivered": "🟢 Yetkazildi", "cancelled": "🔴 Bekor qilindi"}

# Admin paneldan o‘zgartiriladigan BARCHA sozlamalarning boshlang‘ich qiymatlari
DEFAULTS = {
    "name": "MONTERA.UZ", "tagline": "Erkaklar uchun premium kiyimlar", "accent": "#8b6b3d", "orders_enabled": True,
    "closed_text": "Hozir buyurtmalar qabul qilinmayapti. Tez orada qaytamiz.", "announce": "",
    "banner_title": "NEW\nCOLLECTION", "banner_sub": "Erkaklar uchun zamonaviy uslub", "banner_btn": "Ko‘rish", "banner_image": "",
    "phone": "", "telegram": "", "instagram": "", "address": "", "hours": "", "about": "",
    "tashkent": 30000, "region": 50000, "free_from": 0, "min_order": 0,
    "delivery_text": "Toshkent bo‘ylab 1–2 kun, viloyatlarga 2–5 kun ichida yetkazamiz.", "payment_text": "To‘lov: yetkazib berishda naqd",
    "welcome": "<b>{shop}</b> ga xush kelibsiz, {name} 👋\n\nEng yaxshi kiyimlar bir joyda. Pastdagi tugma orqali do‘konni oching 👇",
    "help_text": "💬 Savolingiz bormi? Shu yerga yozing, xabaringiz adminga boradi va javob shu chatga keladi.",
    "categories": ["T-shirt", "Polo", "Sviter", "Hoodie", "Sweatshirt", "Ko‘ylak", "Shim", "Jinsi", "Komplekt", "Kurtka", "Palto", "Aksessuar"],
    "colors": ["Black", "White", "Gray", "Navy", "Beige", "Olive", "Brown", "Blue"],
    "sizes": ["S", "M", "L", "XL", "2XL", "3XL"],
    "extra_admins": [],
}

engine = create_async_engine(DBURL, connect_args={"ssl": True} if E.get("DB_SSL", "1") != "0" else {}, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
num = lambda n: f"{n:,}".replace(",", " ")
esc = html.escape


# ---------- Sozlamalar ----------
async def cfg(s) -> dict:
    rows = {r.key: r.value for r in (await s.scalars(select(Setting))).all()}
    c = {**DEFAULTS}
    c.update({k: v for k, v in (rows.get("delivery") or {}).items() if k in DEFAULTS})   # eski versiyadan
    b = rows.get("banner") or {}
    if b.get("title"): c["banner_title"] = b["title"]
    if b.get("sub"): c["banner_sub"] = b["sub"]
    c.update({k: v for k, v in (rows.get("shop") or {}).items() if k in DEFAULTS})
    return c


async def get_cfg() -> dict:
    async with Session() as s: return await cfg(s)


async def load_admins():
    DYN.clear(); DYN.update(int(x) for x in (await get_cfg()).get("extra_admins", []))


tg_url = lambda u: u if u.startswith("http") else "https://t.me/" + u.lstrip("@/")
ig_url = lambda u: u if u.startswith("http") else "https://instagram.com/" + u.lstrip("@/")


def contact_text(c):
    rows = [("📞", c["phone"]), ("✈️", ("@" + c["telegram"].lstrip("@")) if c["telegram"] and not c["telegram"].startswith("http") else c["telegram"]),
            ("📷", c["instagram"]), ("📍", c["address"]), ("🕘", c["hours"])]
    body = "\n".join(f"{i} {esc(v)}" for i, v in rows if v)
    return f"<b>📞 Aloqa</b>\n\n{body or 'Aloqa ma’lumotlari tez orada qo‘shiladi.'}"


def contact_kb(c):
    r = []
    if c["telegram"]: r.append(InlineKeyboardButton(text="✈️ Telegram", url=tg_url(c["telegram"])))
    if c["instagram"]: r.append(InlineKeyboardButton(text="📷 Instagram", url=ig_url(c["instagram"])))
    rows = [r] if r else []
    rows.append([InlineKeyboardButton(text="💬 Adminga yozish", callback_data="help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Do‘konni ochish", web_app=WebAppInfo(url=URL))],
        [InlineKeyboardButton(text="📦 Buyurtmalarim", callback_data="my"), InlineKeyboardButton(text="📞 Aloqa", callback_data="contact")],
        [InlineKeyboardButton(text="💬 Yordam", callback_data="help")]])


def status_kb(oid):
    b = lambda t, st: InlineKeyboardButton(text=t, callback_data=f"st:{oid}:{st}")
    return InlineKeyboardMarkup(inline_keyboard=[[b("✅ Tasdiqlash", "confirmed"), b("🧵 Tayyorlanmoqda", "preparing")],
                                                 [b("🚚 Yetkazilmoqda", "shipping"), b("🟢 Yetkazildi", "delivered")],
                                                 [b("❌ Bekor qilish", "cancelled")]])


BC = {}  # admin_id -> {"state": "wait"|"ready", "text", "photo"}


def shop_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 Ko‘rish", web_app=WebAppInfo(url=URL))]])


def adm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="a:stats"), InlineKeyboardButton(text="🆕 Yangi buyurtmalar", callback_data="a:new")],
        [InlineKeyboardButton(text="📢 Xabar yuborish", callback_data="a:bc"), InlineKeyboardButton(text="👥 Mijozlar", callback_data="a:users")],
        [InlineKeyboardButton(text="⚙️ To‘liq boshqaruv paneli", web_app=WebAppInfo(url=URL + "/#admin"))]])


def bc_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Yuborish", callback_data="a:bcgo"), InlineKeyboardButton(text="❌ Bekor", callback_data="a:bcno")]])


def order_text(o):
    return (f"🆕 <b>#{o.number}</b> · {SL[o.status]}\n{esc(o.name)} · {esc(o.phone)}\n"
            f"{esc(o.region)}, {esc(o.city)}, {esc(o.address)}\n"
            + "\n".join(f"• {esc(l['name'])} {esc(l['color'])}/{esc(l['size'])} ×{l['qty']}" for l in o.items)
            + (f"\n💬 {esc(o.note)}" if o.note else "") + f"\n<b>Jami: {num(o.total)} so‘m</b>")


async def stats_text():
    async with Session() as s: x = await stats(None, s)
    top = "\n".join(f"{i + 1}. {esc(t['name'])} — {t['sold']} dona" for i, t in enumerate(x["top"])) or "—"
    return (f"📊 <b>Statistika</b>\n\n👥 Mijozlar: {x['users']} (bugun +{x['new_users']})\n📦 Buyurtmalar: {x['orders']} (bugun {x['orders_today']})\n"
            f"💰 Daromad: {num(x['revenue'])} so‘m (bugun {num(x['revenue_today'])})\n\n🏆 Top mahsulotlar:\n{top}")


async def orders_text(uid):
    async with Session() as s:
        rows = (await s.scalars(select(Order).where(Order.user_id == uid).order_by(Order.id.desc()).limit(5))).all()
    if not rows: return "Sizda hali buyurtma yo‘q. Do‘konni ochib, birinchi buyurtmangizni bering 🛍"
    return "<b>📦 Oxirgi buyurtmalaringiz</b>\n\n" + "\n\n".join(f"<b>#{o.number}</b> · {SL[o.status]}\n{num(o.total)} so‘m" for o in rows)


async def setup_bot():
    try:
        c = await get_cfg()
        await bot.set_my_commands([BotCommand(command="start", description="Do‘konni ochish"), BotCommand(command="orders", description="Buyurtmalarim"),
                                   BotCommand(command="contact", description="Aloqa"), BotCommand(command="help", description="Yordam")])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🛍 Do‘kon", web_app=WebAppInfo(url=URL)))
        await bot.set_my_short_description(f"{c['name']} — {c['tagline']}"[:120])
        await bot.set_my_description(f"{c['name']} — {c['tagline']}\n\nBoshlash uchun START tugmasini bosing."[:512])
    except Exception as e:
        print("setup_bot:", e)


async def to_admins(text, photo=None):
    for a in admin_ids():
        try:
            if photo: await bot.send_photo(a, photo, caption=text)
            else: await bot.send_message(a, text)
        except Exception: pass


async def say(uid, text, markup=None):
    try: await bot.send_message(uid, text, reply_markup=markup)
    except Exception: pass


def reply_uid(m: Message):
    r = m.reply_to_message
    src = ((r.text or r.caption or "") if r else "")
    return int(src.split("#uid")[1].split()[0]) if "#uid" in src else None


# ---------- Bot ----------
@dp.message(CommandStart())
async def start(m: Message):
    async with Session() as s:
        u = await s.get(User, m.from_user.id) or User(id=m.from_user.id)
        u.username, u.first_name, u.last_name = m.from_user.username, m.from_user.first_name, m.from_user.last_name
        s.add(u); await s.commit()
        c = await cfg(s)
    await m.answer(c["welcome"].replace("{shop}", esc(c["name"])).replace("{name}", esc(m.from_user.first_name or "")), reply_markup=kb())


@dp.message(Command("id"))
async def cmd_id(m: Message): await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>")


@dp.message(Command("orders"))
async def cmd_orders(m: Message): await m.answer(await orders_text(m.from_user.id))


@dp.message(Command("help"))
async def cmd_help(m: Message): await m.answer((await get_cfg())["help_text"])


@dp.message(Command("contact"))
async def cmd_contact(m: Message):
    c = await get_cfg(); await m.answer(contact_text(c), reply_markup=contact_kb(c))


@dp.callback_query(F.data == "my")
async def cb_my(c: CallbackQuery):
    await c.message.answer(await orders_text(c.from_user.id)); await c.answer()


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.message.answer((await get_cfg())["help_text"]); await c.answer()


@dp.callback_query(F.data == "contact")
async def cb_contact(c: CallbackQuery):
    cf = await get_cfg(); await c.message.answer(contact_text(cf), reply_markup=contact_kb(cf)); await c.answer()


@dp.callback_query(F.data.startswith("st:"))
async def cb_status(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Ruxsat yo‘q", show_alert=True)
    _, oid, st = c.data.split(":")
    async with Session() as s:
        try: o = await change_status(s, int(oid), st)
        except HTTPException: return await c.answer("Xatolik", show_alert=True)
    await c.answer("Status yangilandi, mijozga xabar ketdi ✅")
    try: await c.message.edit_text(order_text(o), reply_markup=status_kb(o.id))
    except Exception: pass


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if is_admin(m.from_user.id): await m.answer("⚙️ <b>Admin panel</b>\nKerakli bo‘limni tanlang:", reply_markup=adm_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(m: Message):
    BC.pop(m.from_user.id, None); await m.answer("Bekor qilindi")


@dp.callback_query(F.data.startswith("a:"))
async def cb_admin(c: CallbackQuery):
    uid = c.from_user.id
    if not is_admin(uid): return await c.answer("Ruxsat yo‘q", show_alert=True)
    act = c.data[2:]; await c.answer()
    if act == "stats": await c.message.answer(await stats_text())
    elif act == "new":
        async with Session() as s:
            rows = (await s.scalars(select(Order).where(Order.status == "new").order_by(Order.id.desc()).limit(5))).all()
        if not rows: await c.message.answer("Yangi buyurtma yo‘q ✅")
        for o in rows: await c.message.answer(order_text(o), reply_markup=status_kb(o.id))
    elif act == "users":
        async with Session() as s:
            total = await s.scalar(select(func.count()).select_from(User))
            last = (await s.scalars(select(User).order_by(User.created_at.desc()).limit(5))).all()
        await c.message.answer(f"👥 <b>Mijozlar: {total}</b>\n\nOxirgilar:\n" + "\n".join(f"• {esc(u.first_name or '—')} {('@' + u.username) if u.username else ''}" for u in last))
    elif act == "bc":
        BC[uid] = {"state": "wait"}
        await c.message.answer("📢 Yuboriladigan xabarni yozing yoki rasm (izoh bilan) yuboring.\nBekor qilish: /cancel")
    elif act == "bcgo":
        j = BC.pop(uid, None)
        if j and j.get("state") == "ready":
            asyncio.create_task(bcast(j["text"], j["photo"])); await c.message.answer("Yuborish boshlandi ✅")
    elif act == "bcno":
        BC.pop(uid, None); await c.message.answer("Bekor qilindi")


@dp.message(F.photo)
async def photo(m: Message):
    uid, fid = m.from_user.id, m.photo[-1].file_id
    if is_admin(uid):
        if BC.get(uid, {}).get("state") == "wait":
            BC[uid] = {"state": "ready", "text": m.caption or "", "photo": fid}
            return await m.answer_photo(fid, caption=(m.caption or "") + "\n\n👆 Shu xabar hammaga yuboriladi.", parse_mode=None, reply_markup=bc_confirm_kb())
        to = reply_uid(m)
        if to:
            try: await bot.send_photo(to, fid, caption=f"💬 {m.caption or ''}", parse_mode=None); await m.reply("Yuborildi ✅")
            except Exception: await m.reply("Yuborib bo‘lmadi")
        else: await m.reply("Mahsulot rasmini qo‘shish: Profil → Boshqaruv paneli → Mahsulotlar.")
        return
    async with Session() as s:
        u = await s.get(User, uid)
        if u and u.blocked: return
    await to_admins(f"📷 <b>{esc(m.from_user.full_name)}</b> #uid{uid}\n{esc(m.caption or '')}\n\n<i>Javob berish uchun shu xabarga reply qiling</i>", fid)
    await m.answer("Rasmingiz adminga yuborildi ✅")


@dp.message(F.text & ~F.text.startswith("/"))
async def talk(m: Message):
    uid = m.from_user.id
    if is_admin(uid):
        if BC.get(uid, {}).get("state") == "wait":
            BC[uid] = {"state": "ready", "text": m.text, "photo": None}
            return await m.answer(m.text + "\n\n👆 Shu xabar hammaga yuboriladi.", parse_mode=None, reply_markup=bc_confirm_kb())
        to = reply_uid(m)
        if to:
            await say(to, f"💬 <b>{esc((await get_cfg())['name'])}:</b>\n{esc(m.text)}"); await m.reply("Yuborildi ✅")
        return
    async with Session() as s:
        u = await s.get(User, uid)
        if u and u.blocked: return
    await to_admins(f"💬 <b>{esc(m.from_user.full_name)}</b> #uid{uid}\n{esc(m.text)}\n\n<i>Javob berish uchun shu xabarga reply qiling</i>")
    await m.answer("Xabaringiz adminga yuborildi ✅ Tez orada javob beramiz.")


# ---------- FastAPI ----------
@asynccontextmanager
async def lifespan(_):
    async with engine.begin() as c: await c.run_sync(Base.metadata.create_all)
    await load_admins()
    if URL:
        await bot.set_webhook(f"{URL}/webhook/{SECRET}", drop_pending_updates=True)
        await setup_bot()
    yield
    await bot.session.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


async def db():
    async with Session() as s: yield s


async def auth(x_init_data: str = Header(""), s: AsyncSession = Depends(db)) -> User:
    d_ = dict(parse_qsl(x_init_data)); h = d_.pop("hash", "")
    key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d_.items()))
    ok = h and hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h)
    if not ok or time.time() - int(d_.get("auth_date", 0)) > 86400 * 3:
        raise HTTPException(401, "Iltimos, do‘konni Telegram bot orqali oching")
    t = json.loads(d_["user"])
    u = await s.get(User, t["id"])
    new = u is None
    if new: u = User(id=t["id"])
    info = (t.get("username"), t.get("first_name"), t.get("last_name"))
    if new or (u.username, u.first_name, u.last_name) != info:
        u.username, u.first_name, u.last_name = info
        s.add(u); await s.commit()
    if u.blocked: raise HTTPException(403, "Siz bloklangansiz")
    return u


def admin(u: User = Depends(auth)):
    if not is_admin(u.id): raise HTTPException(403, "Ruxsat yo‘q")
    return u


def d(o):
    return {c.name: (v.isoformat() if isinstance(v := getattr(o, c.name), datetime) else v) for c in o.__table__.columns}


def promo_off(pr, sub):
    if not pr or not pr.active or (pr.expires and pr.expires < datetime.utcnow()) or (pr.limit and pr.used >= pr.limit): return None
    return min(sub, sub * (pr.percent or 0) // 100 + (pr.fixed or 0))


@app.get("/health")
async def health(): return {"ok": True}


@app.post("/webhook/{secret}")
async def webhook(secret: str, req: Request):
    if secret != SECRET: raise HTTPException(404)
    await dp.feed_update(bot, Update.model_validate(await req.json(), context={"bot": bot}))
    return {"ok": True}


IMG = {}


@app.get("/img/{fid}")
async def img(fid: str):
    if fid not in IMG:
        try: f = await bot.get_file(fid)
        except Exception: raise HTTPException(404)
        async with aiohttp.ClientSession() as c, c.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}") as r:
            data = await r.read()
        if len(IMG) > 150: IMG.pop(next(iter(IMG)))
        IMG[fid] = data
    return Response(IMG[fid], media_type="image/jpeg", headers={"Cache-Control": "public,max-age=31536000"})


# ---------- Mijoz API ----------
@app.get("/api/me")
async def me(u: User = Depends(auth)): return {"id": u.id, "first_name": u.first_name, "username": u.username, "is_admin": is_admin(u.id)}


@app.get("/api/config")
async def config(_=Depends(auth), s=Depends(db)):
    c = await cfg(s); c.pop("extra_admins", None); return c


@app.get("/api/products")
async def products(_=Depends(auth), s=Depends(db)):
    ps = (await s.scalars(select(Product).where(Product.active == True).order_by(Product.id.desc()))).all()
    rt = {i: (a, c) for i, a, c in (await s.execute(select(Review.product_id, func.avg(Review.rating), func.count()).group_by(Review.product_id))).all()}
    return [{**d(p), "rating": round(float(rt[p.id][0]), 1) if p.id in rt else 0, "rcount": rt[p.id][1] if p.id in rt else 0} for p in ps]


@app.get("/api/favorites")
async def favs(u=Depends(auth), s=Depends(db)):
    return (await s.scalars(select(Favorite.product_id).where(Favorite.user_id == u.id))).all()


@app.post("/api/favorites/{pid}")
async def fav(pid: int, u=Depends(auth), s=Depends(db)):
    f = await s.get(Favorite, (u.id, pid))
    if f: await s.delete(f)
    else: s.add(Favorite(user_id=u.id, product_id=pid))
    await s.commit(); return {"fav": not f}


class PromoIn(BaseModel):
    code: str; subtotal: int


@app.post("/api/promo/check")
async def promo_check(b: PromoIn, _=Depends(auth), s=Depends(db)):
    off = promo_off(await s.get(Promo, b.code.strip().upper()), b.subtotal)
    if off is None: raise HTTPException(400, "Promo kod yaroqsiz yoki muddati tugagan")
    return {"code": b.code.strip().upper(), "discount": off}


class Item(BaseModel):
    id: int; color: str; size: str; qty: int = Field(ge=1, le=20)


class OrderIn(BaseModel):
    name: str = Field(min_length=2, max_length=80); phone: str = Field(min_length=7, max_length=20)
    region: str = Field(max_length=60); city: str = Field(min_length=2, max_length=80)
    address: str = Field(min_length=3, max_length=300); note: str = Field("", max_length=300)
    delivery: str = "courier"; promo: str = ""
    items: list[Item] = Field(min_length=1, max_length=30)


@app.post("/api/orders")
async def make_order(b: OrderIn, bg: BackgroundTasks, u=Depends(auth), s=Depends(db)):
    c = await cfg(s)
    if not c["orders_enabled"]: raise HTTPException(400, c["closed_text"])
    ps = {p.id: p for p in (await s.scalars(select(Product).where(Product.id.in_([i.id for i in b.items])).with_for_update())).all()}
    lines, sub = [], 0
    for i in b.items:
        p, k = ps.get(i.id), f"{i.color}|{i.size}"
        if not p or not p.active: raise HTTPException(400, "Mahsulot topilmadi")
        have = (p.stock or {}).get(k, 0)
        if have < i.qty: raise HTTPException(400, f"{p.name} ({i.color}, {i.size}): omborda {have} dona qoldi")
        p.stock = {**(p.stock or {}), k: have - i.qty}; p.sold = (p.sold or 0) + i.qty
        lines.append({"id": p.id, "name": p.name, "color": i.color, "size": i.size, "qty": i.qty, "price": p.price})
        sub += p.price * i.qty
    if c["min_order"] and sub < c["min_order"]: raise HTTPException(400, f"Minimal buyurtma summasi: {num(c['min_order'])} so‘m")
    code = b.promo.strip().upper(); pr = await s.get(Promo, code) if code else None
    off = 0
    if code:
        off = promo_off(pr, sub)
        if off is None: raise HTTPException(400, "Promo kod yaroqsiz")
        pr.used = (pr.used or 0) + 1
    fee = c["tashkent"] if b.region == "Toshkent shahri" else c["region"]
    if c["free_from"] and sub - off >= c["free_from"]: fee = 0
    o = Order(user_id=u.id, name=b.name, phone=b.phone, region=b.region, city=b.city, address=b.address, note=b.note,
              delivery="post" if b.delivery == "post" else "courier", items=lines, subtotal=sub, discount=off, fee=fee,
              total=sub - off + fee, promo=code)
    s.add(o); await s.flush(); o.number = f"MON-{10000 + o.id}"; await s.commit()
    bg.add_task(say, u.id, f"{ST['new']} ✅\nBuyurtma: <b>#{o.number}</b>\nJami: {num(o.total)} so‘m")
    txt = order_text(o)
    for a in admin_ids(): bg.add_task(say, a, txt, status_kb(o.id))
    return d(o)


@app.get("/api/orders")
async def my_orders(u=Depends(auth), s=Depends(db)):
    return [d(o) for o in (await s.scalars(select(Order).where(Order.user_id == u.id).order_by(Order.id.desc()).limit(50))).all()]


@app.post("/api/orders/{oid}/cancel")
async def cancel_order(oid: int, bg: BackgroundTasks, u=Depends(auth), s=Depends(db)):
    o = await s.get(Order, oid)
    if not o or o.user_id != u.id or o.status != "new": raise HTTPException(400, "Bu buyurtmani bekor qilib bo‘lmaydi")
    await change_status(s, oid, "cancelled")
    for a in admin_ids(): bg.add_task(say, a, f"❌ Mijoz <b>#{o.number}</b> buyurtmani bekor qildi.")
    return {"ok": True}


class ContactIn(BaseModel):
    text: str = Field(min_length=2, max_length=1000)


@app.post("/api/contact")
async def contact(b: ContactIn, u=Depends(auth)):
    who = esc(u.first_name or "Mijoz") + (f" @{u.username}" if u.username else "")
    await to_admins(f"💬 <b>{who}</b> #uid{u.id}\n{esc(b.text)}\n\n<i>Javob berish uchun shu xabarga reply qiling</i>")
    return {"ok": True}


class ReviewIn(BaseModel):
    product_id: int; rating: int = Field(ge=1, le=5); text: str = Field("", max_length=500)


@app.get("/api/reviews/{pid}")
async def reviews(pid: int, _=Depends(auth), s=Depends(db)):
    rows = (await s.scalars(select(Review).where(Review.product_id == pid).order_by(Review.id.desc()).limit(30))).all()
    return [{"name": r.name, "rating": r.rating, "text": r.text, "created_at": r.created_at.isoformat()} for r in rows]


@app.post("/api/reviews")
async def add_review(b: ReviewIn, u=Depends(auth), s=Depends(db)):
    done = (await s.scalars(select(Order).where(Order.user_id == u.id, Order.status == "delivered"))).all()
    if not any(l["id"] == b.product_id for o in done for l in o.items):
        raise HTTPException(400, "Faqat yetkazib berilgan mahsulotga baho berish mumkin")
    r = await s.scalar(select(Review).where(Review.user_id == u.id, Review.product_id == b.product_id)) or Review(user_id=u.id, product_id=b.product_id)
    r.name, r.rating, r.text = u.first_name or "Mijoz", b.rating, b.text.strip()
    s.add(r); await s.commit(); return {"ok": True}


# ---------- Admin API ----------
class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=160); description: str = ""; category: str
    price: int = Field(ge=0); old_price: Optional[int] = None
    images: list[str] = []; colors: list[str] = []; sizes: list[str] = []; stock: dict[str, int] = {}
    material: str = ""; sku: str = ""; active: bool = True


@app.get("/api/admin/stats")
async def stats(_=Depends(admin), s=Depends(db)):
    t0 = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0); ok = Order.status != "cancelled"
    n = lambda *w: s.scalar(select(func.count()).select_from(Order).where(*w))
    rev = lambda *w: s.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(ok, *w))
    top = (await s.execute(select(Product.name, Product.sold).order_by(Product.sold.desc()).limit(5))).all()
    rows = (await s.execute(select(Order.created_at, Order.total).where(ok, Order.created_at >= t0 - timedelta(days=6)))).all()
    by_day = {}
    for c_, t in rows: by_day[c_.date()] = by_day.get(c_.date(), 0) + t
    days = [t0.date() - timedelta(days=i) for i in range(6, -1, -1)]
    active = (await s.scalars(select(Product).where(Product.active == True))).all()
    low = [{"name": p.name, "left": sum((p.stock or {}).values())} for p in active if sum((p.stock or {}).values()) <= 3][:8]
    return {"users": await s.scalar(select(func.count()).select_from(User)),
            "new_users": await s.scalar(select(func.count()).select_from(User).where(User.created_at >= t0)),
            "orders": await n(), "orders_today": await n(Order.created_at >= t0), "orders_new": await n(Order.status == "new"),
            "revenue": await rev(), "revenue_today": await rev(Order.created_at >= t0),
            "top": [{"name": a, "sold": b} for a, b in top], "low": low,
            "daily": [{"d": x.strftime("%d.%m"), "v": by_day.get(x, 0)} for x in days]}


@app.get("/api/admin/orders")
async def a_orders(status: str = "", q: str = "", _=Depends(admin), s=Depends(db)):
    st = select(Order).order_by(Order.id.desc()).limit(100)
    if status: st = st.where(Order.status == status)
    if q:
        like = f"%{q.strip()}%"; st = st.where(or_(Order.number.ilike(like), Order.name.ilike(like), Order.phone.ilike(like)))
    rows = (await s.scalars(st)).all()
    ids = {o.user_id for o in rows}
    us = {u.id: u.username for u in (await s.scalars(select(User).where(User.id.in_(ids)))).all()} if ids else {}
    return [{**d(o), "username": us.get(o.user_id)} for o in rows]


class StatusIn(BaseModel):
    status: str


async def change_status(s, oid, status):
    o = await s.get(Order, oid)
    if not o or status not in ST: raise HTTPException(400, "Noto‘g‘ri so‘rov")
    if status == "cancelled" and o.status != "cancelled":  # ombor qaytariladi
        for l in o.items:
            p = await s.get(Product, l["id"])
            if p: k = f"{l['color']}|{l['size']}"; p.stock = {**(p.stock or {}), k: (p.stock or {}).get(k, 0) + l["qty"]}
    o.status = status; await s.commit()
    await say(o.user_id, f"{ST[status]}\n<b>#{o.number}</b>" + ("\n\n⭐ Mahsulotga baho berishingiz mumkin — do‘konni oching." if status == "delivered" else ""))
    return o


@app.post("/api/admin/orders/{oid}/status")
async def a_status(oid: int, b: StatusIn, _=Depends(admin), s=Depends(db)):
    return d(await change_status(s, oid, b.status))


class MsgIn(BaseModel):
    user_id: int; text: str = Field(min_length=1, max_length=1500)


@app.post("/api/admin/message")
async def a_message(b: MsgIn, _=Depends(admin)):
    await say(b.user_id, f"💬 <b>{esc((await get_cfg())['name'])}:</b>\n{esc(b.text)}"); return {"ok": True}


@app.post("/api/admin/upload")
async def a_upload(req: Request, u=Depends(admin)):
    data = await req.body()
    if not data or len(data) > 8_000_000: raise HTTPException(400, "Rasm bo‘sh yoki juda katta")
    m = await bot.send_photo(u.id, BufferedInputFile(data, "photo.jpg"), caption="📷 Rasm saqlandi", disable_notification=True)
    return {"ref": "tg:" + m.photo[-1].file_id}


@app.get("/api/admin/products")
async def a_products(_=Depends(admin), s=Depends(db)):
    return [d(p) for p in (await s.scalars(select(Product).order_by(Product.id.desc()))).all()]


@app.post("/api/admin/products")
async def a_add(b: ProductIn, _=Depends(admin), s=Depends(db)):
    p = Product(**b.model_dump()); s.add(p); await s.commit(); return d(p)


@app.put("/api/admin/products/{pid}")
async def a_edit(pid: int, b: ProductIn, _=Depends(admin), s=Depends(db)):
    p = await s.get(Product, pid)
    if not p: raise HTTPException(404, "Topilmadi")
    for k, v in b.model_dump().items(): setattr(p, k, v)
    await s.commit(); return d(p)


@app.delete("/api/admin/products/{pid}")
async def a_del(pid: int, _=Depends(admin), s=Depends(db)):
    p = await s.get(Product, pid)
    if p: await s.delete(p); await s.commit()
    return {"ok": True}


class PromoNew(BaseModel):
    code: str = Field(min_length=2, max_length=30); percent: int = Field(0, ge=0, le=100); fixed: int = Field(0, ge=0)
    expires: Optional[datetime] = None; limit: Optional[int] = None


@app.get("/api/admin/promos")
async def a_promos(_=Depends(admin), s=Depends(db)):
    return [d(p) for p in (await s.scalars(select(Promo))).all()]


@app.post("/api/admin/promos")
async def a_promo(b: PromoNew, _=Depends(admin), s=Depends(db)):
    await s.merge(Promo(**{**b.model_dump(), "code": b.code.strip().upper(), "active": True})); await s.commit(); return {"ok": True}


@app.post("/api/admin/promos/{code}/toggle")
async def a_ptoggle(code: str, _=Depends(admin), s=Depends(db)):
    p = await s.get(Promo, code)
    if p: p.active = not p.active; await s.commit()
    return {"ok": True}


@app.delete("/api/admin/promos/{code}")
async def a_pdel(code: str, _=Depends(admin), s=Depends(db)):
    p = await s.get(Promo, code)
    if p: await s.delete(p); await s.commit()
    return {"ok": True}


class SettingsIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=40); tagline: Optional[str] = Field(None, max_length=120)
    accent: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$"); orders_enabled: Optional[bool] = None
    closed_text: Optional[str] = Field(None, max_length=200); announce: Optional[str] = Field(None, max_length=160)
    banner_title: Optional[str] = Field(None, max_length=60); banner_sub: Optional[str] = Field(None, max_length=120)
    banner_btn: Optional[str] = Field(None, max_length=24); banner_image: Optional[str] = Field(None, max_length=300)
    phone: Optional[str] = Field(None, max_length=30); telegram: Optional[str] = Field(None, max_length=100)
    instagram: Optional[str] = Field(None, max_length=100); address: Optional[str] = Field(None, max_length=200)
    hours: Optional[str] = Field(None, max_length=100); about: Optional[str] = Field(None, max_length=1000)
    tashkent: Optional[int] = Field(None, ge=0); region: Optional[int] = Field(None, ge=0)
    free_from: Optional[int] = Field(None, ge=0); min_order: Optional[int] = Field(None, ge=0)
    delivery_text: Optional[str] = Field(None, max_length=300); payment_text: Optional[str] = Field(None, max_length=200)
    welcome: Optional[str] = Field(None, max_length=1500); help_text: Optional[str] = Field(None, max_length=1000)
    categories: Optional[list[str]] = Field(None, max_length=40); colors: Optional[list[str]] = Field(None, max_length=40)
    sizes: Optional[list[str]] = Field(None, max_length=30); extra_admins: Optional[list[int]] = Field(None, max_length=20)


@app.get("/api/admin/settings")
async def a_settings(_=Depends(admin), s=Depends(db)): return await cfg(s)


@app.put("/api/admin/settings")
async def a_set_save(b: SettingsIn, bg: BackgroundTasks, _=Depends(admin), s=Depends(db)):
    cur = await s.get(Setting, "shop")
    new = {**(cur.value if cur else {}), **b.model_dump(exclude_unset=True, exclude_none=True)}
    for k in ("categories", "colors", "sizes"):
        if k in new: new[k] = [x.strip() for x in new[k] if x.strip()]
    await s.merge(Setting(key="shop", value=new)); await s.commit()
    DYN.clear(); DYN.update(int(x) for x in new.get("extra_admins", []))
    bg.add_task(setup_bot)
    return await cfg(s)


@app.get("/api/admin/users")
async def a_users(q: str = "", _=Depends(admin), s=Depends(db)):
    st = select(User).order_by(User.created_at.desc()).limit(50)
    if q.strip():
        q = q.strip().lstrip("@"); cond = [User.username.ilike(f"%{q}%"), User.first_name.ilike(f"%{q}%")]
        if q.isdigit(): cond.append(User.id == int(q))
        st = select(User).where(or_(*cond)).limit(50)
    return [d(u) for u in (await s.scalars(st)).all()]


class BlockIn(BaseModel):
    blocked: bool


@app.post("/api/admin/users/{uid}/block")
async def a_block(uid: int, b: BlockIn, _=Depends(admin), s=Depends(db)):
    u = await s.get(User, uid)
    if u: u.blocked = b.blocked; await s.commit()
    return {"ok": True}


class BcIn(BaseModel):
    text: str = Field("", max_length=3500); photo: Optional[str] = None


async def bcast(text, photo=None):
    async with Session() as s: ids = (await s.scalars(select(User.id).where(User.blocked == False))).all()
    fid = photo[3:] if photo and photo.startswith("tg:") else photo
    for i in ids:
        try:
            if fid: await bot.send_photo(i, fid, caption=text or None, reply_markup=shop_kb(), parse_mode=None)
            else: await bot.send_message(i, text, reply_markup=shop_kb(), parse_mode=None)
        except Exception: pass
        await asyncio.sleep(0.05)


@app.post("/api/admin/broadcast")
async def a_bc(b: BcIn, bg: BackgroundTasks, _=Depends(admin)):
    if not b.text.strip() and not b.photo: raise HTTPException(400, "Matn yoki rasm kiriting")
    bg.add_task(bcast, b.text, b.photo); return {"ok": True}


@app.get("/api/admin/reviews")
async def a_reviews(_=Depends(admin), s=Depends(db)):
    rows = (await s.scalars(select(Review).order_by(Review.id.desc()).limit(50))).all()
    names = {p.id: p.name for p in (await s.scalars(select(Product).where(Product.id.in_({r.product_id for r in rows})))).all()} if rows else {}
    return [{**d(r), "product": names.get(r.product_id, "—")} for r in rows]


@app.delete("/api/admin/reviews/{rid}")
async def a_rdel(rid: int, _=Depends(admin), s=Depends(db)):
    r = await s.get(Review, rid)
    if r: await s.delete(r); await s.commit()
    return {"ok": True}


@app.get("/")
async def index(): return FileResponse(Path(__file__).parent / "index.html", headers={"Cache-Control": "no-cache"})
