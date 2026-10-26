# Free Fire Glory Push — Telegram reseller bot (v5)

## Run on Pterodactyl
1. Upload **the contents** of this folder so `main.py` sits next to `requirements.txt` (not in a sub-folder).
2. Startup: **PY_FILE = `main.py`**, **REQUIREMENTS_FILE = `requirements.txt`**. Missing packages install themselves into `.pylibs/`.
3. Settings are read from `.env`. Start the server — admins get "🟢 Bot started" plus a self-check.
4. Updating from v4: just upload over the old files. The database upgrades itself on start (old wallets become NPR
   wallets, old users keep their name and get currency NPR). The old `glory.db` is kept — back it up first anyway.

## What customers see
- **First start:** name and country. No password. Prices and payments are in NPR.
- **Home** (every button colored: green = shop/pay, blue = view, red = cancel): 🛒 SHOP NOW · 🔍 Check SQD available · ⚔️ My Guilds · 🧾 My Orders · 💰 Wallet · 👤 Profile · 📜 Rules · 🛟 Support.
- **Check SQD available:** the real number of squads available (your website credits: basic / premium), per region, with a refresh button.
- **Buy:** region (shows stock) → Guild ID → squads (limited to stock) → pay by **QR (NPR, APINepal)** or wallet; the QR disappears by itself once paid.
  After payment the order is placed **automatically** (no admin step) and the customer gets
  *BOT REQUEST HAVE BEEN SEND – PLEASE ACCEPT BOT REQUEST IN GUILD* with the Details / Important notes.
- **My Guilds:** guild name, live glory with "▲ +N", the 8-hour timer (remaining h:mm / worked h:mm), Refresh and ℹ️ Glory info.
- **Rules:** admin can edit them in ⚙️ Settings → Edit rules.
- **My Orders:** every order as `BNY-1001`, `BNY-1002`… with guild name and 12-hour Nepal time.
- **Wallet:** add balance (preset or custom amount) by QR.
- **Profile:** edit name, country, profile photo.
- If a payment arrives but the order can't be placed, the money goes to the wallet. If only some squads can be started,
  only those are charged and the rest returns to the wallet.

## Admin (`/admin`)
🔴 **Live Orders** (with live glory, controls) · 📦 **Stock & Credits** (website credits, tap a region to mark out of stock,
pause all orders) · 💰 **Prices** (±buttons or exact) · 🎟 **Redeem Code** (redeems a coupon on the
website account = add credits) · 💳 Payments · 👥 Users (wallet adjust, suspend) · 🏆 Groups (restart/stop/delete) ·
📢 Broadcast · 📊 Reports + CSV · ⚙️ Settings (service window, API status, provider notices, premium emoji IDs, audit).
Admins are notified for every paid order and every problem; nobody has to approve anything.

## Service window
Closed from **Sunday 9 PM to Monday 12 AM (Nepal time)**; customers see a notice. Admin → Settings can switch it off.

## Colors and premium emoji
Buttons are colored (green = go, blue = info, red = stop/cancel) on current Telegram apps; older apps show plain buttons.
Premium emoji on buttons: Admin → Settings → "Get premium emoji IDs", then set `PREMIUM_EMOJI_BUTTONS=true` and
`PREMIUM_EMOJI_IDS=buy=ID,stock=ID,...` (names: buy, stock, guild, orders, wallet, profile, support, refresh, cancel, pay, ok, home, fire, trophy).

## Safety rules
Same payment never makes two orders; wrong amount → no order + alert; the provider has no idempotency key, so a create call
is never blindly re-sent — unclear answers are checked against the group list (before/after); "busy" answers are retried;
anything still unclear becomes *Needs a look* for the admin.

## Tests
`pip install -r requirements-dev.txt && pytest`

## 🌐 BNY SHOP website (new in v8)
A neon website that works like the bot — same accounts, wallets, orders and stock (it runs inside the bot process).
- **Login:** username + password, "Create account", or **Telegram code** (send `/weblogin` to the bot → 8-digit one-time code →
  you get the *same* wallet and orders on the site). Telegram users can then set a username/password on the Profile page.
- **SHOP NOW (5 steps):** Region → Guild ID → Check → Squads → Pay (QR or wallet) → "BOT REQUEST HAVE BEEN SEND".
- **Buy credit:** Wallet → Add balance → QR (APINepal), auto-detected, auto-credited.
- **My Guilds:** live glory with ▲ change, the 8-hour clock, glory info. **My Orders**, **Stock**, **Rules**, **Support**, **Profile**.
- **Admin** (only Telegram IDs in `ADMIN_IDS`; log in with `/weblogin`): overview + price ±, stock on/off + pause, redeem code,
  live orders, users (suspend / wallet adjust), edit rules, broadcast.
- **Hosting:** it listens on the panel's port (`SERVER_PORT`, or `WEB_PORT`). In Pterodactyl add a second **allocation/port** to the
  server and point your domain/proxy (Cloudflare, nginx…) to `IP:port`. Use HTTPS in front of it. Set `WEB_ENABLED=false` to turn it off.
- Safety: provider/payment keys stay on the server; passwords are scrypt-hashed; sessions are random tokens (hash stored);
  CSRF header + SameSite cookie; login rate-limit; security headers (CSP).
