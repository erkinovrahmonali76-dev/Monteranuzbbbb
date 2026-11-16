# Do‘kon boti (Telegram Mini App)

## Muhit o‘zgaruvchilari
| Nomi | Izoh |
|---|---|
| BOT_TOKEN | @BotFather tokeni |
| DATABASE_URL | PostgreSQL manzili |
| ADMIN_ID | Asosiy admin(lar) Telegram ID, vergul bilan: `123,456` |
| TELEGRAM_WEBAPP_URL | Sayt manzili (https://...) — bo‘lmasa RENDER_EXTERNAL_URL ishlatiladi |
| SECRET_KEY | Webhook uchun maxfiy so‘z |
| DB_SSL | `0` qo‘ysangiz SSL o‘chadi (lokal baza uchun) |

Ishga tushirish: `pip install -r requirements.txt` → `uvicorn main:app --host 0.0.0.0 --port 10000`

## Admin hamma narsani o‘zgartira oladi
Botda `/admin` → «To‘liq boshqaruv paneli» (yoki ilovada Profil → Boshqaruv paneli):
- **Statistika** — daromad, 7 kunlik grafik, top va tugayotgan mahsulotlar
- **Buyurtmalar** — filtr, qidiruv, status, mijozga xabar yozish
- **Mahsulotlar** — qo‘shish / tahrirlash / yashirish / o‘chirish, rasm, ombor
- **Mijozlar** — qidiruv, xabar, bloklash
- **Promo** — yaratish, muddat, limit, yoqish/o‘chirish
- **Sharhlar** — ko‘rish va o‘chirish
- **Sozlamalar** — do‘kon nomi, rang, e’lon satri, banner (matn+rasm), aloqa (telefon, Telegram, Instagram, manzil, ish vaqti),
  yetkazish narxi, bepul yetkazish, minimal buyurtma, buyurtmani to‘xtatish, botning salomlashuv va yordam matni,
  kategoriya/rang/razmerlar, qo‘shimcha adminlar
- **Xabar** — hammaga matn yoki rasm yuborish
