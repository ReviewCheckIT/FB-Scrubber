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

load_dotenv()

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Pro Scraper Bot is Running!", 200

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
        ref.child(safe_key).set(group_data)
        return True
    except:
        return False

# --- গ্রুপের বিস্তারিত তথ্য এবং অটো-অ্যাপ্রুভ চেক ---
def get_group_details(page, group_link):
    details = {
        "status": "Unknown",
        "members": "Not Found",
        "admin_link": "Not Found",
        "name": "FB Group"
    }
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        # ১. গ্রুপের নাম ও মেম্বার সংখ্যা সংগ্রহ
        details["name"] = page.title().replace(" | Facebook", "")
        content = page.content()
        
        if "members" in content.lower():
            try:
                # মেম্বার সংখ্যা খুঁজে বের করা
                details["members"] = page.locator("xpath=//span[contains(text(), 'members')]").first.inner_text()
            except: pass

        # ২. অটো-অ্যাপ্রুভ চেক করা
        post_box = page.get_by_text("Write something...", exact=False).or_(page.get_by_text("Create a public post...", exact=False))
        if post_box.is_visible():
            post_box.click()
            time.sleep(2)
            check_text = page.content().lower()
            if "admin approval" in check_text or "must be approved" in check_text:
                details["status"] = "❌ Admin Approval Required"
            else:
                details["status"] = "✅ Auto-Approve"
            # পোস্ট বক্স বন্ধ করা
            page.keyboard.press("Escape")
        else:
            details["status"] = "❌ Private/Restricted"

        # ৩. মেইন এডমিন লিংক সংগ্রহ (About সেকশন থেকে)
        try:
            page.goto(f"{group_link}/about", wait_until="domcontentloaded")
            time.sleep(3)
            # এডমিনদের প্রোফাইল লিংক খোঁজা
            admin_loc = page.locator("a[href*='/user/']").first
            if admin_loc.is_visible():
                details["admin_link"] = admin_loc.get_attribute("href").split('?')[0]
        except: pass

    except Exception as e:
        print(f"Detail Fetch Error: {e}")
    
    return details

# --- মেইন স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country, chat_id, bot_instance):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
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
            time.sleep(5)
            
            for _ in range(3): # স্ক্রলিং
                page.mouse.wheel(0, 1000)
                time.sleep(3)

            links = page.locator("a[href*='/groups/']").all()
            unique_links = []
            for l in links:
                href = l.get_attribute("href")
                if href and "/groups/" in href:
                    clean = href.split('?')[0].rstrip('/')
                    if "/user/" not in clean and clean not in unique_links:
                        unique_links.append(clean)

            bot_instance.send_message(chat_id, f"🔍 মোট {len(unique_links)}টি গ্রুপ পাওয়া গেছে। বিস্তারিত তথ্য যাচাই করা হচ্ছে...")

            for link in unique_links[:15]: # Render-এর জন্য একবারে ১৫টি নিরাপদ
                info = get_group_details(page, link)
                data = {
                    "name": info["name"],
                    "link": link,
                    "members": info["members"],
                    "status": info["status"],
                    "admin": info["admin_link"],
                    "keyword": keyword,
                    "found_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                save_to_firebase(data)
                
                # সুন্দর ফরম্যাটে মেসেজ পাঠানো
                msg = (f"📁 **Group Name:** {data['name']}\n"
                       f"👥 **Members:** {data['members']}\n"
                       f"🛠 **Status:** {data['status']}\n"
                       f"🔗 **Link:** {data['link']}\n"
                       f"👤 **Admin:** {data['admin']}\n"
                       f"---------------------------")
                bot_instance.send_message(chat_id, msg, disable_web_page_preview=True)

        except Exception as e:
            bot_instance.send_message(chat_id, f"❌ এরর: {str(e)}")
        finally:
            browser.close()

# --- টেলিগ্রাম কমান্ডস ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **Pro Group Scraper v2.0**\n\nদেশের নাম লিখুন:")

@bot.message_handler(commands=['export'])
def export_data(message):
    ref = db.reference('groups')
    data = ref.get()
    if data:
        df = pd.DataFrame(list(data.values()))
        df.to_csv("groups_data.csv", index=False)
        with open("groups_data.csv", 'rb') as f:
            bot.send_document(message.chat.id, f)
    else:
        bot.send_message(message.chat.id, "ডাটাবেস খালি!")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "কি-ওয়ার্ড লিখুন:")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 প্রসেসিং শুরু হয়েছে... একটু সময় দিন।")
    scrape_facebook(keyword, country, chat_id, bot)
    bot.send_message(chat_id, "✅ স্ক্র্যাপিং শেষ!")
    del user_states[chat_id]

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.infinity_polling()
