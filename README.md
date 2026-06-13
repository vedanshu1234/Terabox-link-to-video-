# Terabox-link-to-video-

# 📦 TeraBox Bot — Render.com Free Hosting Guide

> Terabox link → file/video seedha Telegram mein.
> GPLink ads ke saath monetize karo. **Bilkul free host karo Render pe.**

---

## ⚡ 15-Minute Deploy Guide

### Step 1 — GitHub pe Code Upload karo

Pehle apna code GitHub pe daalna hoga.

**GitHub account nahi hai?** → [github.com](https://github.com) pe free account banao.

```bash
# Apni machine pe (ya GitHub website se bhi kar sakte ho):
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/AAPKA_USERNAME/terabox-bot.git
git push -u origin main
```

> 💡 **GitHub Desktop** use kar sakte ho agar command line nahi aati —
> [desktop.github.com](https://desktop.github.com) — bilkul simple drag & drop.

---

### Step 2 — Render.com Account Banao

1. **[render.com](https://render.com)** pe jao
2. **"Get Started for Free"** click karo
3. **GitHub se login karo** (yehi easiest hai)
4. Credit card nahi maangega — bilkul free!

---

### Step 3 — PostgreSQL Database Banao (Free)

> Database zaruri hai kyunki Render free tier mein local file storage nahi hoti.

1. Render Dashboard mein **"New +"** click karo
2. **"PostgreSQL"** select karo
3. Ye settings bharein:
   - **Name:** `terabox-bot-db`
   - **Region:** `Singapore` (India ke paas)
   - **Plan:** `Free`
4. **"Create Database"** click karo
5. Database banne ke baad — **"Internal Database URL"** copy karo aur kahin save karo

```
postgresql://terabox_bot_db_user:PASSWORD@HOST/terabox_bot_db
```

---

### Step 4 — Web Service Banao

1. Dashboard mein **"New +"** → **"Web Service"**
2. **"Connect a repository"** → apna GitHub repo select karo
3. Ye settings bharein:

| Setting | Value |
|---------|-------|
| **Name** | `terabox-bot` |
| **Region** | `Singapore` |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Plan** | `Free` |

4. **"Advanced"** section mein jaao → **"Add Environment Variable"**

---

### Step 5 — Environment Variables Set Karo

"Environment Variables" section mein **har ek variable** add karo:

| Key | Value | Kahan milega |
|-----|-------|-------------|
| `BOT_TOKEN` | `123456:ABC...` | @BotFather → /newbot |
| `DATABASE_URL` | `postgresql://...` | Step 3 mein copy kiya tha |
| `GPLINK_API` | `abc123...` | gplinks.com → Tools → Developer API |
| `PORT` | `10000` | Aisa hi rakhein |
| `FREE_LINKS` | `3` | Badal sakte ho |
| `COOLDOWN_HRS` | `7` | Badal sakte ho |
| `COOKIE` | _(khali ya apni cookie)_ | Optional |

> ⚠️ `WEBHOOK_URL` abhi mat dalo — pehle deploy hone do

5. **"Create Web Service"** click karo

---

### Step 6 — WEBHOOK_URL Set Karo

Deploy hone ke baad (2-3 minute wait karo):

1. Render aapki service ka URL dikhayega:
   ```
   https://terabox-bot-xxxx.onrender.com
   ```
2. Dashboard mein service ke **"Environment"** tab mein jao
3. **"Add Environment Variable"** karo:
   - Key: `WEBHOOK_URL`
   - Value: `https://terabox-bot-xxxx.onrender.com` ← apna URL
4. **"Save Changes"** → service automatically redeploy hogi

---

### Step 7 — Test Karo!

1. Telegram pe apne bot mein jao
2. `/start` bhejo
3. Koi Terabox link paste karo
4. 🎉 Kaam karna chahiye!

**Deploy failed?** → Render Dashboard → service → **"Logs"** tab dekho

---

## 🔑 GPLink API Key Kaise Milegi

1. [gplinks.com](https://gplinks.com) pe **free account** banao
2. Login karo → upar menu mein **"Tools"** → **"Developer API"**
3. API key copy karo → Render mein `GPLINK_API` mein paste karo

> Jab bhi koi user ad link kholta hai → aapko earning hoti hai 💰

---

## 🍪 TeraBox Cookie (Recommended)

Cookie se bot zyada reliable hota hai (badi files ke liye especially):

1. Browser mein [terabox.com](https://terabox.com) login karo
2. `F12` dabao → **Application** tab → **Storage** → **Cookies**
3. `terabox.com` ke neeche `ndus` ki value copy karo
4. Render mein `COOKIE` = `ndus=xxxxxx` set karo

---

## ⚠️ Important: Render Free Tier Ki Limitations

| Limitation | Solution |
|------------|----------|
| **15 min baad service so jaati hai** | Bot mein **self-ping** built-in hai — auto jaag jaayega |
| **SQLite disk nahi milta** | Hum **PostgreSQL** use kar rahe hain |
| **PostgreSQL 90 din ke baad expire** | Naya free DB banao ya paid upgrade karo |
| **750 hours/month compute** | Ek bot ke liye kaafi hai (24×31 = 744 hrs) |
| **Pehli request slow (cold start)** | ~1 minute lag sakta hai agar koi kaafi der se nahi aaya |

### Self-Ping Kya Hai?

Bot khud apne aap ko har 14 minute mein ping karta hai taaki Render use so na jaane dein. Yeh feature **bot.py mein already built-in** hai — kuch alag karne ki zarurat nahi.

---

## 🔄 Code Update Kaise Karein

```bash
# Code edit karo → phir:
git add .
git commit -m "update"
git push
```

Render automatically detect karega aur redeploy kar dega! 🚀

---

## 🔧 Troubleshooting

**Bot `/start` pe respond nahi kar raha?**
1. Render logs dekho (Dashboard → service → Logs)
2. `BOT_TOKEN` sahi hai?
3. `WEBHOOK_URL` set hai aur sahi URL hai?
4. Ek baar manually redeploy karo (Dashboard → Manual Deploy)

**"Link kaam nahi kar raha" error?**
→ Link expire/private ho sakta hai. `COOKIE` add karo.

**Database error?**
→ `DATABASE_URL` sahi copy kiya? (Internal URL, External nahi)

**GPLink ad nahi ban raha?**
→ `GPLINK_API` check karo. GPLink dashboard pe API key verify karo.

**PostgreSQL 90 din baad?**
→ Purana DB delete karo → naya banao → nayi `DATABASE_URL` Render mein update karo.

---

## 📊 User Experience (Telegram mein kya dikhega)

```
User:  [Terabox link bheja]
Bot:   ✅ File aa rahi hai...  (1st, 2nd, 3rd — free)
       [File/Video milti hai]

User:  [4th link bheja]
Bot:   📢 3 free links use ho gaye!
       [🎬 Ad Dekho & File Pao]  ← GPLink button
       Ad ke baad 7 ghante mein phir 3 free links!

User:  [Bina ad ke 5th link bheja]
Bot:   ⏳ 6 ghante 40 minute baaki hain. /status dekho.

User:  /status
Bot:   ⏳ 6 ghante 37 minute baad reset hoga.
```

---

## 📁 Project Files

```
terabox-bot/
├── bot.py            ← Poora bot (Terabox + GPLink + PostgreSQL + self-ping)
├── requirements.txt  ← Python dependencies
├── render.yaml       ← Render Blueprint (optional shortcut)
├── env.example       ← Local testing ke liye template
├── Dockerfile        ← Docker deployment (optional)
├── .gitignore        ← .env ko Git se bahar rakhta hai
└── README.md         ← Yeh file
```

---

## 📝 License

MIT — freely use, modify, deploy karo.
