import os
import time
import json
import threading
import telebot
import pandas as pd
from flask import Flask
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
load_dotenv()

app = Flask(__name__)

@app.route('/')
def health_check():
    return "🔥 Pro Scraper Bot is Running Successfully!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

# ফায়ারবেস ইনিশিয়ালাইজেশন
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        if not firebase_admin._apps:
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})
    except Exception as e:
        print(f"Firebase Init Error: {e}")

def save_to_firebase(group_data, category):
    """ডাটাবেসে ক্যাটাগরি অনুযায়ী সেভ করার ফাংশন"""
    try:
        # ক্যাটাগরি অনুযায়ী পাথ সেট করা (auto_approve অথবা admin_approve)
        ref = db.reference(f'groups/{category}')
        safe_key = group_data['link'].replace('.', '_').replace('/', '|').replace(':', '')
        ref.child(safe_key).set(group_data)
        return True
    except Exception as e:
        print(f"Firebase Save Error: {e}")
        return False

# --- গ্রুপের বিস্তারিত তথ্য এবং অটো-অ্যাপ্রুভ চেক ---
def get_group_details(page, group_link):
    details = {
        "status": "Unknown",
        "members": "N/A",
        "admin_link": "N/A",
        "name": "Facebook Group",
        "is_auto": False
    }
    try:
        # গ্রুপ পেজে যাওয়া
        page.goto(group_link, wait_until="networkidle", timeout=60000)
        time.sleep(3)

        # ১. গ্রুপের নাম ও মেম্বার সংখ্যা
        try:
            details["name"] = page.title().split('|')[0].strip()
            # মেম্বার সংখ্যা খোঁজা
            member_element = page.locator("xpath=//span[contains(text(), 'members')]").first
            if member_element.is_visible():
                details["members"] = member_element.inner_text()
        except: pass

        # ২. অটো-অ্যাপ্রুভ চেক (পোস্ট বক্সের মাধ্যমে)
        try:
            # বিভিন্ন ভাষায় পোস্ট বক্সের টেক্সট চেক
            post_triggers = ["Write something...", "Create a public post...", "আপনি কিছু লিখুন...", "একটি পাবলিক পোস্ট তৈরি করুন..."]
            post_box = None
            for trigger in post_triggers:
                target = page.get_by_text(trigger, exact=False)
                if target.is_visible():
                    post_box = target
                    break

            if post_box:
                post_box.click()
                time.sleep(3)
                dialog_content = page.content().lower()
                
                # যদি "admin approval" বা "approved by admin" শব্দ থাকে
                if any(x in dialog_content for x in ["admin approval", "must be approved", "অ্যাডমিন অনুমোদন"]):
                    details["status"] = "❌ Admin Approval Required"
                    details["is_auto"] = False
                else:
                    details["status"] = "✅ Auto-Approve"
                    details["is_auto"] = True
                
                page.keyboard.press("Escape")
            else:
                details["status"] = "🔒 Private/Restricted"
        except:
            details["status"] = "⚠️ Could not verify"

        # ৩. এডমিন লিংক সংগ্রহ (About সেকশন)
        try:
            about_url = f"{group_link}/about"
            page.goto(about_url, wait_until="domcontentloaded")
            time.sleep(2)
            # প্রোফাইল ইউজার লিংক ফিল্টার করা
            admin_loc = page.locator("a[href*='/user/'], a[href*='profile.php']").first
            if admin_loc.is_visible():
                details["admin_link"] = "https://www.facebook.com" + admin_loc.get_attribute("href").split('?')[0]
        except: pass

    except Exception as e:
        print(f"Detail Fetch Error for {group_link}: {e}")
    
    return details

# --- মেইন স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, chat_id, bot_instance):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # লগইন প্রক্রিয়া
            page.goto("https://www.facebook.com/login", timeout=90000)
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            
            # লগইন চেক
            time.sleep(10)
            if "login" in page.url:
                bot_instance.send_message(chat_id, "❌ ফেসবুক লগইন ব্যর্থ হয়েছে! ইমেইল বা পাসওয়ার্ড চেক করুন।")
                return

            # সার্চ প্রক্রিয়া
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, timeout=90000)
            time.sleep(5)
            
            # কয়েকবার স্ক্রল করা যাতে আরও গ্রুপ আসে
            for _ in range(3):
                page.mouse.wheel(0, 1500)
                time.sleep(2)

            # গ্রুপের লিংকগুলো সংগ্রহ
            links_elements = page.locator("a[href*='/groups/']").all()
            unique_links = []
            for l in links_elements:
                href = l.get_attribute("href")
                if href and "/groups/" in href:
                    clean_link = href.split('?')[0].rstrip('/')
                    if "/user/" not in clean_link and clean_link not in unique_links:
                        if clean_link.endswith('/'): clean_link = clean_link[:-1]
                        unique_links.append(clean_link)

            bot_instance.send_message(chat_id, f"🔍 মোট {len(unique_links)}টি গ্রুপ পাওয়া গেছে। এখন ফিল্টারিং শুরু হচ্ছে...")

            # প্রতিটি গ্রুপের বিস্তারিত তথ্য সংগ্রহ
            for link in unique_links[:20]: # সীমাবদ্ধতা ২০টি যাতে আইডি ব্লক না হয়
                info = get_group_details(page, link)
                
                data = {
                    "name": info["name"],
                    "link": link,
                    "members": info["members"],
                    "status": info["status"],
                    "admin": info["admin_link"],
                    "keyword": keyword,
                    "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }

                # ক্যাটাগরি নির্ধারণ
                category = "auto_approve" if info["is_auto"] else "admin_approve"
                save_to_firebase(data, category)
                
                # টেলিগ্রামে মেসেজ পাঠানো
                icon = "🟢" if info["is_auto"] else "🔴"
                msg = (f"{icon} **{data['name']}**\n"
                       f"👥 সদস্য: {data['members']}\n"
                       f"🛠 ধরন: {data['status']}\n"
                       f"👤 এডমিন: {data['admin']}\n"
                       f"🔗 লিংক: {data['link']}\n"
                       f"---------------------------")
                bot_instance.send_message(chat_id, msg, disable_web_page_preview=True)
                time.sleep(1) # রেট লিমিট এড়াতে

        except Exception as e:
            bot_instance.send_message(chat_id, f"❌ স্ক্র্যাপিং এরর: {str(e)}")
        finally:
            browser.close()

# --- টেলিগ্রাম বট কমান্ডস ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    welcome_msg = (
        "🚀 **Pro Group Scraper v2.5**\n\n"
        "এটি উন্নত অটো-ফিল্টারিং বট।\n"
        "শুরু করতে দেশের নাম অথবা টার্গেট এরিয়া লিখুন (যেমন: USA):"
    )
    bot.reply_to(message, welcome_msg, parse_mode="Markdown")

@bot.message_handler(commands=['export'])
def export_data(message):
    try:
        ref = db.reference('groups')
        all_data = ref.get()
        if not all_data:
            bot.send_message(message.chat.id, "📭 ডাটাবেসে কোনো তথ্য নেই!")
            return

        # ডাটা প্রসেস করে CSV বানানো
        rows = []
        for cat, groups in all_data.items():
            for g_id, g_info in groups.items():
                g_info['category'] = cat
                rows.append(g_info)
        
        df = pd.DataFrame(rows)
        file_path = "fb_groups_leads.csv"
        df.to_csv(file_path, index=False)
        
        with open(file_path, 'rb') as f:
            bot.send_document(message.chat.id, f, caption="📂 বর্তমান সকল লিড এক্সপোর্ট করা হয়েছে।")
        os.remove(file_path)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ এক্সপোর্ট এরর: {e}")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'location': message.text}
    bot.reply_to(message, "🎯 এখন আপনার **কি-ওয়ার্ড** লিখুন (যেমন: Buy and Sell):")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    location = user_states[chat_id]['location']
    keyword = f"{message.text} {location}"
    
    bot.send_message(chat_id, f"⏳ কাজ শুরু হয়েছে...\n📍 লোকেশন: {location}\n🔑 কি-ওয়ার্ড: {message.text}\n\nএটি সম্পন্ন হতে কয়েক মিনিট সময় নিতে পারে।")
    
    # থ্রেডিং এর মাধ্যমে স্ক্র্যাপিং চালানো যাতে বট রেসপন্স দিতে পারে
    thread = threading.Thread(target=scrape_facebook, args=(keyword, chat_id, bot))
    thread.start()
    
    del user_states[chat_id]

if __name__ == "__main__":
    # ওয়েব সার্ভার ব্যাকগ্রাউন্ডে চালানো (Render-এর জন্য)
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot is Polling...")
    bot.infinity_polling()
