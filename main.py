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
    return "Bot is alive!", 200

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
        safe_key = group_data['link'].replace('.', '_').replace('/', '|').replace(':', '')
        if not ref.child(safe_key).get():
            ref.child(safe_key).set(group_data)
            return True
        return False
    except:
        return False

# --- মানুষের মতো টাইপিং ---
def human_type(element, text):
    for char in text:
        element.type(char, delay=random.uniform(100, 250))

# --- গ্রুপ স্ট্যাটাস চেক ---
def check_approval_status(page, group_link):
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=30000)
        time.sleep(random.uniform(3, 5))
        content = page.content().lower()
        
        # অটো-অ্যাপ্রুভ কি না তা বোঝার কি-ওয়ার্ড
        admin_indicators = ["admin approval", "posts must be approved", "submitted for approval", "অনুমোদনের জন্য"]
        if any(ind in content for ind in admin_indicators):
            return "Admin Approve ⏳"
        return "Auto Approve ✅"
    except:
        return "Manual Check Required ⚠️"

# --- মেইন স্ক্র্যাপার ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        
        # সেশন ফাইল থাকলে সেটি লোড করবে
        if os.path.exists(SESSION_FILE):
            context = browser.new_context(storage_state=SESSION_FILE, user_agent="Mozilla/5.0...")
        else:
            context = browser.new_context(user_agent="Mozilla/5.0...")

        page = context.new_page()

        try:
            # ১. লগইন চেক
            page.goto("https://www.facebook.com/groups/feed/", wait_until="domcontentloaded")
            if "login" in page.url:
                print("Logging in...")
                page.goto("https://www.facebook.com/login")
                page.fill("input[name='email']", FB_EMAIL)
                page.fill("input[name='pass']", FB_PASSWORD)
                page.keyboard.press("Enter")
                page.wait_for_timeout(10000)
                context.storage_state(path=SESSION_FILE) # সেশন সেভ

            # ২. সার্চ
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, wait_until="networkidle")
            
            # স্ক্রলিং
            for _ in range(3):
                page.mouse.wheel(0, 800)
                time.sleep(2)

            # ৩. লিঙ্ক সংগ্রহ (উন্নত সিলেক্টর)
            group_elements = page.locator('//a[contains(@href, "/groups/") and not(contains(@href, "/user/"))]').all()
            
            temp_list = []
            seen_links = set()

            for el in group_elements:
                try:
                    href = el.get_attribute("href").split('?')[0].rstrip('/')
                    name = el.inner_text().split('\n')[0]
                    if "/groups/" in href and href not in seen_links and len(name) > 2:
                        temp_list.append({"name": name, "link": href})
                        seen_links.add(href)
                except: continue

            # ৪. স্ট্যাটাস চেক (প্রথম ৫-১০টি গ্রুপের জন্য)
            for item in temp_list[:10]:
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
        finally:
            browser.close()
    return results

# --- টেলিগ্রাম বট ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "👋 দেশের নাম লিখুন (যেমন: USA):")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এখন Keyword লিখুন (যেমন: Freelancing):")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 {country}-তে '{keyword}' এর গ্রুপ খোঁজা হচ্ছে...")
    
    try:
        groups = scrape_facebook(keyword, country)
        if groups:
            new_count = 0
            for g in groups:
                if save_to_firebase(g):
                    new_count += 1
                    msg = f"📌 **{g['name']}**\n✅ স্ট্যাটাস: `{g['status']}`\n🔗 {g['link']}"
                    bot.send_message(chat_id, msg, parse_mode="Markdown")
            bot.send_message(chat_id, f"✅ কাজ শেষ! {new_count}টি নতুন গ্রুপ ডেটাবেসে যোগ হয়েছে।")
        else:
            bot.send_message(chat_id, "❌ কোনো নতুন গ্রুপ পাওয়া যায়নি। কিওয়ার্ড পরিবর্তন করে চেষ্টা করুন।")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ ত্রুটি: {str(e)}")
    
    del user_states[chat_id]

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.infinity_polling()
