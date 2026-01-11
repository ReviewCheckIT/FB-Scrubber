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

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
load_dotenv()

# --- নেটওয়ার্ক স্ট্যাবিলিটি সেটিংস ---
apihelper.SESSION_TIME_OUT = 120 

# --- পোর্ট বাইন্ডিংয়ের জন্য Flask সেটআপ ---
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

# --- গ্রুপ অ্যাপ্রুভাল স্ট্যাটাস চেক করার ফাংশন ---
def check_approval_status(page, group_link):
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(random.uniform(3, 5))
        
        # ফেসবুকের বিভিন্ন টেক্সট যা দিয়ে বোঝা যায় পোস্ট এডমিন এপ্রুভ করবে কিনা
        # সাধারণত পোস্ট বক্সের আশেপাশে এই টেক্সট থাকে
        content = page.content().lower()
        
        indicator_texts = [
            "submit a public post for admin approval",
            "posts must be approved by an admin",
            "admin approval",
            "pending approval",
            "এডমিন অনুমোদন"
        ]
        
        if any(text in content for text in indicator_texts):
            return "Admin Approve ⏳"
        else:
            return "Auto Approve ✅"
    except:
        return "Unknown ⚠️"

# --- স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, 
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage", 
                "--disable-gpu",
                "--disable-notifications"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # ফেসবুক লগইন
            page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=90000)
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            time.sleep(10) 

            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(random.uniform(5, 8))

            # স্ক্রলিং
            for i in range(4): # টাইম বাঁচাতে স্ক্রলিং কিছুটা কমানো হয়েছে কারণ এখন ভেতরে ঢুকতে হবে
                page.mouse.wheel(0, random.randint(900, 1500))
                print(f"Scrolling page... {i+1}")
                time.sleep(random.uniform(3, 6))

            group_elements = page.locator("a[href*='/groups/']").all()
            seen_links = set()
            
            # সব লিংক আগে সংগ্রহ করা
            temp_links = []
            for link_loc in group_elements:
                try:
                    href = link_loc.get_attribute("href")
                    if href and "/groups/" in href:
                        clean_link = href.split('?')[0].rstrip('/')
                        if clean_link not in seen_links:
                            name = link_loc.inner_text().split('\n')[0]
                            if name and len(name) > 2:
                                temp_links.append({"name": name, "link": clean_link})
                                seen_links.add(clean_link)
                except:
                    continue

            # এখন প্রতিটি গ্রুপে ঢুকে স্ট্যাটাস চেক করা (প্রথম ১০-১৫ টি গ্রুপ চেক করবে স্প্যাম এড়াতে)
            for item in temp_links[:15]: 
                print(f"Checking status for: {item['name']}")
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

# --- টেলিগ্রাম বট লজিক ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **FB Group Scraper Bot**\n\nপ্রথমে দেশের নাম লিখুন (যেমন: UK বা USA):")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, f"দেশ: {message.text}\nএখন আপনার Niche বা Keyword লিখুন:")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 {country}-তে '{keyword}' এর গ্রুপ ও অ্যাপ্রুভাল স্ট্যাটাস খোঁজা হচ্ছে। এতে কিছুটা সময় লাগতে পারে...")
    
    try:
        found_groups = scrape_facebook(keyword, country)
        new_count = 0
        if found_groups:
            for g in found_groups:
                if save_to_firebase(g):
                    new_count += 1
                    # এখানে স্ট্যাটাসসহ মেসেজ পাঠানো হচ্ছে
                    msg = f"📌 **{g['name']}**\n📊 স্ট্যাটাস: `{g['status']}`\n🔗 {g['link']}"
                    bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
            bot.send_message(chat_id, f"✅ কাজ শেষ! {new_count}টি নতুন গ্রুপ পাওয়া গেছে।")
        else:
            bot.send_message(chat_id, "দুঃখিত, কোনো নতুন গ্রুপ খুঁজে পাওয়া যায়নি।")
    except Exception as e:
        bot.send_message(chat_id, f"❌ স্ক্র্যাপিংয়ে সমস্যা হয়েছে: {str(e)}")
    
    if chat_id in user_states:
        del user_states[chat_id]

# --- মেইন এক্সিকিউশন ---
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot is starting and ready for action...")
    
    while True:
        try:
            bot.polling(non_stop=True, interval=2, timeout=120)
        except Exception as e:
            print(f"Polling error occurred: {e}")
            time.sleep(10)
