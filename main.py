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

def save_to_firebase(group_data, category):
    try:
        # এখানে category অনুযায়ী ডাটাবেসের আলাদা পাথে সেভ হবে
        ref = db.reference(f'groups/{category}')
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
        "name": "FB Group",
        "is_auto": False
    }
    try:
        # টাইমআউট বাড়িয়ে ৬০ সেকেন্ড করা হলো যাতে নেটওয়ার্কের কারণে ফেইল না করে
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # ১. গ্রুপের নাম ও মেম্বার সংখ্যা সংগ্রহ
        details["name"] = page.title().replace(" | Facebook", "")
        
        # মেম্বার সংখ্যা বের করার জন্য এক্সপাথ আরও উন্নত করা হলো
        try:
            member_element = page.locator("xpath=//span[contains(text(), 'members')]").first
            if member_element.is_visible():
                details["members"] = member_element.inner_text()
        except: pass

        # ২. অটো-অ্যাপ্রুভ চেক করা
        post_box = page.get_by_text("Write something...", exact=False).or_(page.get_by_text("Create a public post...", exact=False))
        if post_box.is_visible():
            post_box.click()
            time.sleep(3)
            check_text = page.content().lower()
            # এডমিন অ্যাপ্রুভালের কী-ওয়ার্ডগুলো চেক করা
            if any(x in check_text for x in ["admin approval", "must be approved", "অ্যাডমিন অনুমোদন", "approving posts"]):
                details["status"] = "❌ Admin Approval Required"
                details["is_auto"] = False
            else:
                details["status"] = "✅ Auto-Approve"
                details["is_auto"] = True
            # পোস্ট বক্স বন্ধ করা
            page.keyboard.press("Escape")
        else:
            details["status"] = "❌ Private/Restricted"
            details["is_auto"] = False

        # ৩. মেইন এডমিন লিংক সংগ্রহ (About সেকশন থেকে)
        try:
            page.goto(f"{group_link}/about", wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            # এডমিনদের প্রোফাইল লিংক খোঁজা (ইউজার বা প্রোফাইল আইডি)
            admin_loc = page.locator("a[href*='/user/'], a[href*='profile.php']").first
            if admin_loc.is_visible():
                href = admin_loc.get_attribute("href")
                details["admin_link"] = "https://www.facebook.com" + href.split('?')[0]
        except: pass

    except Exception as e:
        print(f"Detail Fetch Error: {e}")
    
    return details

# --- মেইন স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country, chat_id, bot_instance):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # লগইন
            page.goto("https://www.facebook.com/login", timeout=90000)
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            time.sleep(10)

            # লগইন সফল হয়েছে কিনা চেক
            if "login" in page.url:
                bot_instance.send_message(chat_id, "❌ লগইন ব্যর্থ! আপনার ইমেইল এবং পাসওয়ার্ড চেক করুন।")
                return

            # সার্চ
            search_url = f"https://www.facebook.com/search/groups/?q={keyword} {country}"
            page.goto(search_url, timeout=90000)
            time.sleep(5)
            
            for _ in range(4): # স্ক্রলিং বাড়ানো হলো
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

            bot_instance.send_message(chat_id, f"🔍 মোট {len(unique_links)}টি গ্রুপ পাওয়া গেছে। ফিল্টারিং শুরু হচ্ছে...")

            for link in unique_links[:20]: # একবারে ২০টি গ্রুপ প্রসেস করবে
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
                
                # ক্যাটাগরি অনুযায়ী ফোল্ডারে সেভ (auto_approve অথবা admin_approve)
                category = "auto_approve" if info["is_auto"] else "admin_approve"
                save_to_firebase(data, category)
                
                # সুন্দর ফরম্যাটে মেসেজ পাঠানো
                msg = (f"📁 **Name:** {data['name']}\n"
                       f"👥 **Members:** {data['members']}\n"
                       f"🛠 **Status:** {data['status']}\n"
                       f"🔗 **Link:** {data['link']}\n"
                       f"👤 **Admin:** {data['admin']}\n"
                       f"---------------------------")
                bot_instance.send_message(chat_id, msg, disable_web_page_preview=True)

        except Exception as e:
            bot_instance.send_message(chat_id, f"❌ স্ক্র্যাপিং এরর: {str(e)}")
        finally:
            browser.close()

# --- টেলিগ্রাম কমান্ডস ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **Pro Group Scraper v3.0**\n\nদেশের নাম লিখুন (যেমন: USA):")

@bot.message_handler(commands=['export'])
def export_data(message):
    ref = db.reference('groups')
    data = ref.get()
    if data:
        all_records = []
        # উভয় ক্যাটাগরির ডাটা এক্সপোর্ট ফাইলে যুক্ত করা
        for cat in ['auto_approve', 'admin_approve']:
            if cat in data:
                for key in data[cat]:
                    record = data[cat][key]
                    record['Category'] = cat
                    all_records.append(record)
        
        df = pd.DataFrame(all_records)
        df.to_csv("leads_data.csv", index=False)
        with open("leads_data.csv", 'rb') as f:
            bot.send_document(message.chat.id, f, caption="✅ ডাটাবেস থেকে সকল লিড রপ্তানি করা হয়েছে।")
    else:
        bot.send_message(message.chat.id, "ডাটাবেস খালি!")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "কি-ওয়ার্ড লিখুন (যেমন: Freelancing):")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"🔍 প্রসেসিং শুরু হয়েছে... একটু সময় দিন।")
    
    # স্ক্র্যাপিং থ্রেডে চালানো হয়েছে যাতে বট রেসপন্স করে
    threading.Thread(target=scrape_facebook, args=(keyword, country, chat_id, bot)).start()
    
    del user_states[chat_id]

if __name__ == "__main__":
    # ওয়েব সার্ভার ব্যাকগ্রাউন্ডে চালানো (Render-এর জন্য)
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.infinity_polling()
