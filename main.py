import os
import time
import json
import threading
import telebot
import pandas as pd
from flask import Flask
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
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

# ফায়ারবেস ইনিশিয়ালাইজেশন
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
        app_id = "fb-scraper-pro" # একটি ইউনিক আইডি হিসেবে ব্যবহার করা
        ref = db.reference(f'groups/{category}')
        # লিংকে থাকা অবৈধ ক্যারেক্টার পরিষ্কার করা
        safe_key = "".join(c for c in group_data['link'] if c.isalnum())
        ref.child(safe_key).set(group_data)
        return True
    except Exception as e:
        print(f"Database Save Error: {e}")
        return False

# --- গ্রুপের বিস্তারিত তথ্য সংগ্রহ ---
def get_group_details(page, group_link):
    details = {
        "status": "অজানা",
        "members": "পাওয়া যায়নি",
        "admin_link": "পাওয়া যায়নি",
        "name": "ফেসবুক গ্রুপ",
        "is_auto": False
    }
    try:
        page.goto(group_link, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # ১. গ্রুপের নাম সংগ্রহ
        details["name"] = page.title().split('|')[0].strip()
        
        # ২. মেম্বার সংখ্যা ডিটেকশন (মাল্টিপল সিলেক্টর ট্রাই করা)
        member_selectors = [
            "span:has-text('members')",
            "span:has-text('সদস্য')",
            "a[href*='members']"
        ]
        for selector in member_selectors:
            try:
                elem = page.locator(selector).first
                if elem.is_visible():
                    details["members"] = elem.inner_text()
                    break
            except: continue

        # ৩. অটো-অ্যাপ্রুভ চেক
        # পাবলিক গ্রুপে পোস্ট বক্স চেক করা
        post_selectors = ["text='Write something...'", "text='Create a public post...'", "text='কিছু লিখুন...'"]
        found_box = False
        for sel in post_selectors:
            if page.locator(sel).is_visible():
                page.locator(sel).click()
                found_box = True
                break
        
        if found_box:
            time.sleep(2)
            page_content = page.content().lower()
            # যদি 'admin approval' বা 'approving' শব্দ থাকে তবে এটি অটো নয়
            if any(x in page_content for x in ["admin approval", "approved by admin", "অ্যাডমিন অনুমোদন"]):
                details["status"] = "❌ এডমিন এপ্রুভাল প্রয়োজন"
                details["is_auto"] = False
            else:
                details["status"] = "✅ অটো-এপ্রুভ গ্রুপ"
                details["is_auto"] = True
            page.keyboard.press("Escape")
        else:
            details["status"] = "🔒 প্রাইভেট বা রেস্ট্রিক্টেড"
            details["is_auto"] = False

        # ৪. এডমিন লিংক সংগ্রহ (About পেজ থেকে)
        try:
            page.goto(f"{group_link}/about", wait_until="domcontentloaded", timeout=30000)
            admin_link_elem = page.locator("a[href*='/user/'], a[href*='profile.php']").first
            if admin_link_elem.is_visible():
                details["admin_link"] = "https://facebook.com" + admin_link_elem.get_attribute("href").split('?')[0]
        except: pass

    except Exception as e:
        print(f"Error fetching details for {group_link}: {e}")
    
    return details

# --- মেইন স্ক্র্যাপার ---
def scrape_facebook(keyword, country, chat_id, bot_instance):
    with sync_playwright() as p:
        # ব্রাউজার লঞ্চ (ডিটেকশন এড়াতে আর্গুমেন্ট সহ)
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", 
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled"
        ])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        try:
            # লগইন প্রসেস
            bot_instance.send_message(chat_id, "🔑 ফেসবুকে লগইন করা হচ্ছে...")
            page.goto("https://www.facebook.com/login", timeout=60000)
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            
            # চেক করা লগইন হয়েছে কি না
            page.wait_for_timeout(10000)
            if "login" in page.url:
                bot_instance.send_message(chat_id, "❌ লগইন ব্যর্থ! ইমেইল বা পাসওয়ার্ড ভুল অথবা টু-ফ্যাক্টর অন করা।")
                return

            # সার্চিং
            search_query = f"{keyword} {country}"
            search_url = f"https://www.facebook.com/search/groups/?q={search_query}"
            bot_instance.send_message(chat_id, f"🔍 '{search_query}' লিখে সার্চ করা হচ্ছে...")
            page.goto(search_url, timeout=60000)
            
            # স্ক্রল ডাউন করে গ্রুপ লোড করা
            for _ in range(3):
                page.keyboard.press("End")
                time.sleep(3)

            # লিংক কালেকশন
            group_links = []
            links = page.locator("a[href*='/groups/']").all()
            for l in links:
                href = l.get_attribute("href")
                if href and "/groups/" in href:
                    clean_link = href.split('?')[0].rstrip('/')
                    if clean_link not in group_links and "search" not in clean_link:
                        group_links.append(clean_link)
            
            total_found = len(group_links)
            bot_instance.send_message(chat_id, f"✅ {total_found}টি গ্রুপ পাওয়া গেছে। তথ্য যাচাই শুরু হচ্ছে...")

            # প্রতিটি গ্রুপের ডিটেইলস চেক
            count = 0
            for link in group_links[:15]: # লিমিট ১৫টি যাতে রেন্ডার টাইমআউট না হয়
                info = get_group_details(page, link)
                data = {
                    "name": info["name"],
                    "link": link,
                    "members": info["members"],
                    "status": info["status"],
                    "admin": info["admin_link"],
                    "keyword": keyword,
                    "time": time.strftime("%Y-%m-%d %H:%M")
                }
                
                category = "auto_approve" if info["is_auto"] else "admin_approve"
                save_to_firebase(data, category)
                
                # রেজাল্ট পাঠানো
                msg = (f"💎 **{data['name']}**\n"
                       f"👥 সদস্য: {data['members']}\n"
                       f"🛠 অবস্থা: {data['status']}\n"
                       f"🔗 লিংক: {data['link']}\n"
                       f"👤 এডমিন: {data['admin']}")
                bot_instance.send_message(chat_id, msg, disable_web_page_preview=True)
                count += 1
            
            bot_instance.send_message(chat_id, f"🏁 স্ক্র্যাপিং শেষ! মোট {count}টি গ্রুপের ডাটা সেভ করা হয়েছে।")

        except Exception as e:
            bot_instance.send_message(chat_id, f"❌ এরর: {str(e)}")
        finally:
            browser.close()

# --- টেলিগ্রাম হ্যান্ডলার ---
bot = telebot.TeleBot(BOT_TOKEN)
user_data = {}

@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "👋 স্বাগতম! গ্রুপ স্ক্র্যাপ করতে দেশের নাম দিন (যেমন: UK):")

@bot.message_handler(commands=['export'])
def handle_export(message):
    try:
        ref = db.reference('groups')
        db_data = ref.get()
        if not db_data:
            bot.send_message(message.chat.id, "এখনো কোনো ডাটা সেভ করা হয়নি।")
            return
            
        final_list = []
        for cat in db_data:
            for item_key in db_data[cat]:
                record = db_data[cat][item_key]
                record['Category'] = cat
                final_list.append(record)
        
        df = pd.DataFrame(final_list)
        file_name = "leads_export.csv"
        df.to_csv(file_name, index=False)
        with open(file_name, 'rb') as doc:
            bot.send_document(message.chat.id, doc, caption="📂 ডাটাবেসের সকল লিড এক্সপোর্ট করা হলো।")
    except Exception as e:
        bot.send_message(message.chat.id, f"এক্সপোর্ট এরর: {e}")

@bot.message_handler(func=lambda m: True)
def handle_input(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        user_data[chat_id] = {'country': message.text}
        bot.reply_to(message, "এখন কি-ওয়ার্ড দিন (যেমন: Pet Lovers):")
    else:
        country = user_data[chat_id]['country']
        keyword = message.text
        bot.send_message(chat_id, f"🚀 কাজ শুরু হচ্ছে...\nদেশ: {country}\nবিষয়: {keyword}")
        
        # থ্রেডিং ব্যবহার করে স্ক্র্যাপার রান করা
        threading.Thread(target=scrape_facebook, args=(keyword, country, chat_id, bot)).start()
        del user_data[chat_id]

if __name__ == "__main__":
    # ওয়েব সার্ভার চালু করা (Render/Heroku পোর্টের জন্য)
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot is starting...")
    bot.infinity_polling()
