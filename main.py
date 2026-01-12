import os
import time
import random
import json
import threading
import telebot
from flask import Flask
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

load_dotenv()

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")
SESSION_FILE = "fb_session.json"

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- ফায়ারবেস সেটআপ ---
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})
    except Exception as e:
        print(f"Firebase Error: {e}")

def save_to_firebase(group_data):
    try:
        ref = db.reference('groups')
        # লিঙ্কের স্পেশাল ক্যারেক্টার ক্লিন করা
        safe_key = group_data['link'].replace('.', '_').replace('/', '|').replace(':', '')
        if not ref.child(safe_key).get():
            ref.child(safe_key).set(group_data)
            return True
        return False
    except:
        return False

# --- অটো-অ্যাপ্রুভ স্ট্যাটাস চেক ---
def check_approval_status(page, group_link):
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(3, 5))
        content = page.content().lower()
        
        # কি-ওয়ার্ড চেক
        admin_indicators = ["admin approval", "posts must be approved", "submitted for approval", "অনুমোদনের জন্য", "রিভিউ"]
        if any(ind in content for ind in admin_indicators):
            return "Admin Approve ⏳"
        return "Auto Approve ✅"
    except:
        return "Manual Check Required ⚠️"

# --- মেইন স্ক্র্যাপার (আপডেটেড সিলেক্টর) ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        
        # সেশন ফাইল থাকলে সেটি লোড করবে
        storage = SESSION_FILE if os.path.exists(SESSION_FILE) else None
        context = browser.new_context(
            storage_state=storage,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # ১. লগইন স্ট্যাটাস চেক
            page.goto("https://www.facebook.com/groups/feed/", wait_until="domcontentloaded")
            if "login" in page.url:
                print("Logging in to Facebook...")
                page.goto("https://www.facebook.com/login")
                page.fill("input[name='email']", FB_EMAIL)
                page.fill("input[name='pass']", FB_PASSWORD)
                page.keyboard.press("Enter")
                page.wait_for_timeout(10000)
                context.storage_state(path=SESSION_FILE)

            # ২. সার্চ রেজাল্টে যাওয়া
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, wait_until="networkidle")
            
            # ৩. স্ক্রলিং (আরও নিখুঁতভাবে)
            for _ in range(4):
                page.keyboard.press("PageDown")
                time.sleep(2)

            # ৪. জাভাস্ক্রিপ্ট দিয়ে লিঙ্ক সংগ্রহ (এটি সবচেয়ে বেশি কার্যকর)
            groups = page.evaluate('''() => {
                const links = Array.from(document.querySelectorAll('a[href*="/groups/"]'));
                return links.map(a => ({
                    href: a.href,
                    text: a.innerText
                })).filter(item => 
                    item.text.length > 2 && 
                    !item.href.includes('/user/') && 
                    !item.href.includes('/posts/') &&
                    !item.href.includes('/categories/')
                );
            }''')

            seen_links = set()
            temp_data = []
            for g in groups:
                clean_link = g['href'].split('?')[0].rstrip('/')
                if clean_link not in seen_links:
                    name = g['text'].split('\n')[0]
                    temp_data.append({"name": name, "link": clean_link})
                    seen_links.add(clean_link)

            # ৫. লিমিটেড চেক (প্রথম ১০টি গ্রুপ)
            for item in temp_data[:10]:
                status = check_approval_status(page, item['link'])
                results.append({
                    **item,
                    "status": status,
                    "keyword": keyword,
                    "country": country,
                    "found_at": time.strftime("%Y-%m-%d %H:%M:%S")
                })

        except Exception as e:
            print(f"Error: {e}")
            page.screenshot(path="debug_error.png") # এরর হলে স্ক্রিনশট নিবে
        finally:
            browser.close()
    return results

# --- টেলিগ্রাম বট ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **FB Group Scraper Active!**\n\nদেশের নাম লিখুন (যেমন: USA):")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এখন আপনার Niche বা Keyword লিখুন:")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 '{keyword}' এর অটো-অ্যাপ্রুভ গ্রুপ খোঁজা হচ্ছে...")
    
    try:
        found_groups = scrape_facebook(keyword, country)
        if found_groups:
            new_count = 0
            for g in found_groups:
                if save_to_firebase(g):
                    new_count += 1
                    msg = f"📌 **{g['name']}**\n✅ স্ট্যাটাস: `{g['status']}`\n🔗 {g['link']}"
                    bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
            bot.send_message(chat_id, f"✅ কাজ শেষ! {new_count}টি নতুন গ্রুপ পাওয়া গেছে।")
        else:
            bot.send_message(chat_id, "❌ কোনো গ্রুপ পাওয়া যায়নি। কিওয়ার্ড পরিবর্তন করে আবার চেষ্টা করুন।")
    except Exception as e:
        bot.send_message(chat_id, f"❌ সমস্যা: {str(e)}")
    
    if chat_id in user_states:
        del user_states[chat_id]

if __name__ == "__main__":
    # ওয়েব সার্ভার ব্যাকগ্রাউন্ডে চালানো
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot is polling...")
    bot.infinity_polling()
