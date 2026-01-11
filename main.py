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
from telebot import apihelper

load_dotenv()
apihelper.SESSION_TIME_OUT = 120
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and scraping!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

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

# --- মানুষের মতো টাইপ করার ফাংশন ---
def human_type(element, text):
    for char in text:
        element.type(char, delay=random.uniform(100, 300))
        time.sleep(random.uniform(0.1, 0.3))

# --- গ্রুপ স্ট্যাটাস চেক (মানুষের মতো আচরণ) ---
def check_approval_status(page, group_link):
    try:
        # গ্রুপে ঢোকার আগে মানুষের মতো একটু অপেক্ষা
        time.sleep(random.uniform(2, 5))
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        
        # পেজ স্ক্রল করা (যেন মানুষ পড়ছে)
        page.mouse.wheel(0, random.randint(300, 600))
        time.sleep(random.uniform(3, 5))

        content = page.content().lower()
        admin_indicators = ["admin approval", "posts must be approved", "submitted for approval", "অনুমোদনের জন্য"]
        
        if any(indicator in content for indicator in admin_indicators):
            return "Admin Approve ⏳"
        else:
            return "Auto Approve ✅"
    except:
        return "Manual Check Required ⚠️"

# --- মেইন স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # ১. লগইন (মানুষের মতো টাইপিং)
            page.goto("https://www.facebook.com/login", wait_until="networkidle")
            time.sleep(random.uniform(2, 4))
            
            email_field = page.locator("input[name='email']")
            pass_field = page.locator("input[name='pass']")
            
            human_type(email_field, FB_EMAIL)
            time.sleep(random.uniform(1, 2))
            human_type(pass_field, FB_PASSWORD)
            
            page.keyboard.press("Enter")
            time.sleep(10) # লগইন হওয়ার সময়

            # ২. সার্চ করা
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, wait_until="networkidle")
            time.sleep(random.uniform(5, 7))

            # ৩. স্ক্রলিং
            for i in range(3):
                page.mouse.wheel(0, random.randint(800, 1200))
                time.sleep(random.uniform(3, 5))

            group_links = page.locator("a[href*='/groups/']").all()
            temp_data = []
            seen_links = set()

            for link_loc in group_links:
                try:
                    href = link_loc.get_attribute("href")
                    if href and "/groups/" in href:
                        clean_link = href.split('?')[0].rstrip('/')
                        if clean_link not in seen_links:
                            name = link_loc.inner_text().split('\n')[0]
                            if name and len(name) > 2:
                                temp_data.append({"name": name, "link": clean_link})
                                seen_links.add(clean_link)
                except: continue

            # ৪. প্রতিটি গ্রুপের স্ট্যাটাস চেক করা
            for item in temp_data[:10]: # সুরক্ষার জন্য ১০টি গ্রুপ
                status = check_approval_status(page, item['link'])
                results.append({
                    "name": item['name'],
                    "link": item['link'],
                    "status": status,
                    "keyword": keyword,
                    "country": country,
                    "found_at": time.strftime("%Y-%m-%d %H:%M:%S")
                })
                
        except Exception as e:
            print(f"Scraping Error: {e}")
        finally:
            browser.close()
    return results

# --- টেলিগ্রাম বট ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **FB Group Scraper (Human Mode)**\n\nদেশের নাম লিখুন:")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এখন আপনার Niche বা Keyword লিখুন:")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 '{keyword}' এর গ্রুপ ও অটো-অ্যাপ্রুভ স্ট্যাটাস চেক করা হচ্ছে...")
    
    try:
        found_groups = scrape_facebook(keyword, country)
        new_count = 0
        if found_groups:
            for g in found_groups:
                if save_to_firebase(g):
                    new_count += 1
                    msg = f"📌 **{g['name']}**\n✅ স্ট্যাটাস: `{g['status']}`\n🔗 {g['link']}"
                    bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
            bot.send_message(chat_id, f"✅ কাজ শেষ! {new_count}টি নতুন গ্রুপ পাওয়া গেছে।")
        else:
            bot.send_message(chat_id, "দুঃখিত, কোনো নতুন গ্রুপ পাওয়া যায়নি।")
    except Exception as e:
        bot.send_message(chat_id, f"❌ সমস্যা: {str(e)}")
    
    if chat_id in user_states:
        del user_states[chat_id]

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    while True:
        try:
            bot.polling(non_stop=True, interval=2, timeout=120)
        except Exception as e:
            time.sleep(10)
