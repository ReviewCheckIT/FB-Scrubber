import os
import time
import random
import json
import threading
import telebot
import pandas as pd
from flask import Flask
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
from telebot import apihelper

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
load_dotenv()

# --- নেটওয়ার্ক স্ট্যাবিলিটি সেটিংস ---
apihelper.SESSION_TIME_OUT = 120

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Auto-Approve Bot is active!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

# --- ফায়ারবেস ইনিশিয়ালাইজেশন ---
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})
    except Exception as e:
        print(f"Firebase Init Error: {e}")

def save_to_firebase(group_data):
    try:
        ref = db.reference('groups')
        safe_key = group_data['link'].replace('.', '_').replace('/', '|').replace(':', '')
        if not ref.child(safe_key).get():
            ref.child(safe_key).set(group_data)
            return True
        return False
    except Exception as e:
        print(f"Database Error: {e}")
        return False

# --- অটো-অ্যাপ্রুভ চেক করার অ্যাডভান্সড ফাংশন ---
def is_auto_approve(page, group_link):
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        
        # 'Write something' বা পোস্ট বক্স খোঁজা
        post_box = page.get_by_text("Write something...", exact=False).or_(page.get_by_text("Create a public post...", exact=False))
        
        if post_box.is_visible():
            post_box.click()
            time.sleep(2)
            
            # এডমিন অ্যাপ্রুভাল টেক্সট চেক করা
            content = page.content().lower()
            if "submit a post for admin approval" in content or "posts must be approved by an admin" in content:
                print(f"Skipping: {group_link} (Admin Approval Required)")
                return False
            else:
                print(f"Match Found: {group_link} (Auto-Approve)")
                return True
        return False
    except:
        return False

# --- স্ক্র্যাপিং ফাংশন (অটো-অ্যাপ্রুভ ফিল্টার সহ) ---
def scrape_facebook(keyword, country, chat_id, bot_instance):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()

        try:
            # লগইন
            page.goto("https://www.facebook.com/login", timeout=90000)
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            time.sleep(10)

            # সার্চ
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, timeout=90000)
            
            for _ in range(5): # স্ক্রলিং
                page.mouse.wheel(0, 1000)
                time.sleep(3)

            # সব গ্রুপ লিংক সংগ্রহ
            links = page.locator("a[href*='/groups/']").all()
            unique_links = []
            for l in links:
                href = l.get_attribute("href")
                if href and "/groups/" in href:
                    clean = href.split('?')[0].rstrip('/')
                    if clean not in unique_links: unique_links.append(clean)

            bot_instance.send_message(chat_id, f"🔍 মোট {len(unique_links)}টি গ্রুপ পাওয়া গেছে। এখন অটো-অ্যাপ্রুভ চেক করা হচ্ছে...")

            # প্রতিটি গ্রুপ চেক করা
            for link in unique_links[:20]: # প্রসেসিং লিমিট ২০টি (Render এর জন্য নিরাপদ)
                name_loc = page.locator(f"a[href*='{link.split('/')[-1]}']").first
                name = name_loc.inner_text().split('\n')[0] if name_loc.is_visible() else "FB Group"
                
                if is_auto_approve(page, link):
                    data = {
                        "name": name,
                        "link": link,
                        "keyword": keyword,
                        "country": country,
                        "type": "Auto-Approve",
                        "found_at": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    if save_to_firebase(data):
                        results.append(data)
                        bot_instance.send_message(chat_id, f"✅ **Auto-Approve Found!**\n📌 {name}\n🔗 {link}", disable_web_page_preview=True)

        except Exception as e:
            print(f"Error: {e}")
        finally:
            browser.close()
    return results

# --- টেলিগ্রাম বট লজিক ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **Auto-Approve Scraper**\n\nপ্রথমে দেশের নাম লিখুন:")

@bot.message_handler(commands=['export'])
def export_data(message):
    bot.send_message(message.chat.id, "📊 ডাটাবেস থেকে ফাইল তৈরি করা হচ্ছে...")
    try:
        ref = db.reference('groups')
        data = ref.get()
        if data:
            df = pd.DataFrame(list(data.values()))
            file_name = "fb_auto_approve_groups.csv"
            df.to_csv(file_name, index=False)
            with open(file_name, 'rb') as f:
                bot.send_document(message.chat.id, f)
            os.remove(file_name)
        else:
            bot.send_message(message.chat.id, "ডাটাবেস খালি!")
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এখন আপনার Keyword লিখুন:")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 {keyword} এর অটো-অ্যাপ্রুভ গ্রুপ খোঁজা হচ্ছে। এটি কিছুটা সময় নেবে...")
    
    scrape_facebook(keyword, country, chat_id, bot)
    bot.send_message(chat_id, "✅ স্ক্র্যাপিং শেষ হয়েছে।")
    del user_states[chat_id]

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    while True:
        try:
            bot.polling(non_stop=True, interval=2, timeout=120)
        except:
            time.sleep(10)
