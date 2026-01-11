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
    return "ফেসবুক স্ক্র্যাপার বট রান করছে!", 200

def run_web_server():
    # Render সাধারণত 10000 পোর্টে রান করে
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_COOKIES_JSON = os.getenv("FB_COOKIES") # Render থেকে এই কুকি লোড হবে
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

# ফায়ারবেস ডাটাবেস ইনিশিয়ালাইজেশন
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
        ref = db.reference(f'groups/{category}')
        # লিংকে ইনভ্যালিড ক্যারেক্টার থাকলে ফায়ারবেস পাথ এরর দেয়, তাই ক্লিন করা হচ্ছে
        safe_key = "".join(filter(str.isalnum, group_data['link']))
        ref.child(safe_key).set(group_data)
        return True
    except Exception as e:
        print(f"Database Error: {e}")
        return False

# --- গ্রুপের বিস্তারিত তথ্য সংগ্রহ ---
def get_group_details(page, group_link):
    details = {
        "status": "অজানা",
        "members": "পাওয়া যায়নি",
        "admin_link": "পাওয়া যায়নি",
        "name": "FB Group",
        "is_auto": False
    }
    try:
        page.goto(group_link, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        # গ্রুপের নাম সংগ্রহ
        details["name"] = page.title().split('|')[0].strip()
        
        # মেম্বার সংখ্যা ডিটেকশন
        member_selectors = ["span:has-text('members')", "span:has-text('সদস্য')", "a[href*='members']"]
        for s in member_selectors:
            try:
                elem = page.locator(s).first
                if elem.is_visible():
                    details["members"] = elem.inner_text()
                    break
            except: continue

        # অটো-এপ্রুভ চেক
        # পাবলিক গ্রুপে পোস্ট করার অপশন চেক করা হচ্ছে
        post_selectors = ["Write something...", "Create a public post...", "কিছু লিখুন..."]
        found_box = False
        for selector in post_selectors:
            try:
                target = page.get_by_text(selector).first
                if target.is_visible():
                    target.click()
                    found_box = True
                    break
            except: continue
        
        if found_box:
            time.sleep(2)
            page_content = page.content().lower()
            # এডমিন এপ্রুভালের কোনো টেক্সট আছে কিনা যাচাই
            if any(word in page_content for word in ["admin approval", "approving posts", "অ্যাডমিন অনুমোদন", "review post"]):
                details["status"] = "❌ এডমিন এপ্রুভাল প্রয়োজন"
                details["is_auto"] = False
            else:
                details["status"] = "✅ অটো-এপ্রুভ"
                details["is_auto"] = True
            # পোস্ট বক্স বন্ধ করা
            page.keyboard.press("Escape")
        else:
            details["status"] = "🔒 প্রাইভেট বা সীমাবদ্ধ"

        # এডমিন লিংক খোঁজা
        try:
            page.goto(f"{group_link}/about", wait_until="domcontentloaded", timeout=30000)
            admin_loc = page.locator("a[href*='/user/'], a[href*='profile.php']").first
            if admin_loc.is_visible():
                details["admin_link"] = "https://facebook.com" + admin_loc.get_attribute("href").split('?')[0]
        except: pass

    except Exception as e:
        print(f"Detail Fetch Error: {e}")
    
    return details

# --- মেইন স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country, chat_id, bot_instance):
    with sync_playwright() as p:
        # রেন্ডারের জন্য ব্রাউজার অপশন
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        
        # কুকি ফাইল বা ভেরিয়েবল থেকে কুকি যুক্ত করা
        if FB_COOKIES_JSON:
            try:
                cookies = json.loads(FB_COOKIES_JSON)
                context.add_cookies(cookies)
            except Exception as e:
                bot_instance.send_message(chat_id, f"❌ কুকি ফরম্যাটে সমস্যা: {e}")
                return
        else:
            bot_instance.send_message(chat_id, "❌ Render-এ 'FB_COOKIES' ভেরিয়েবল সেট করা নেই!")
            return

        page = context.new_page()

        try:
            # কুকি কাজ করছে কিনা পরীক্ষা করা
            page.goto("https://www.facebook.com/profile.php", timeout=60000)
            if "login" in page.url:
                bot_instance.send_message(chat_id, "❌ কুকি কাজ করছে না! সম্ভবত এটি এক্সপায়ার হয়ে গেছে। নতুন কুকি দিন।")
                return

            # সার্চ প্রসেস
            search_query = f"{keyword} {country}"
            bot_instance.send_message(chat_id, f"🔍 '{search_query}' দিয়ে সার্চ করা হচ্ছে...")
            page.goto(f"https://www.facebook.com/search/groups/?q={search_query}", timeout=60000)
            
            # কয়েকবার স্ক্রল ডাউন করা যাতে বেশি গ্রুপ পাওয়া যায়
            for _ in range(3):
                page.mouse.wheel(0, 1000)
                time.sleep(2)

            # গ্রুপ লিংক সংগ্রহ
            elements = page.locator("a[href*='/groups/']").all()
            links_to_process = []
            for e in elements:
                href = e.get_attribute("href")
                if href and "/groups/" in href:
                    clean_link = href.split('?')[0].rstrip('/')
                    if clean_link not in links_to_process and "/user/" not in clean_link:
                        links_to_process.append(clean_link)

            bot_instance.send_message(chat_id, f"✅ মোট {len(links_to_process)}টি গ্রুপ পাওয়া গেছে। ফিল্টারিং শুরু হচ্ছে...")

            # ডিটেইলস স্ক্র্যাপিং শুরু
            for link in links_to_process[:15]: # লোড কমাতে শুরুতে ১৫টি লিমিট করা হয়েছে
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
                
                # অটো-এপ্রুভ অনুযায়ী ক্যাটাগরি করা
                category = "auto_approve" if info["is_auto"] else "admin_approve"
                save_to_firebase(data, category)
                
                # রেজাল্ট মেসেজ
                msg = (f"📂 **{data['name']}**\n"
                       f"👥 সদস্য: {data['members']}\n"
                       f"🛠 অবস্থা: {data['status']}\n"
                       f"🔗 লিংক: {data['link']}\n"
                       f"👤 এডমিন: {data['admin']}")
                bot_instance.send_message(chat_id, msg, disable_web_page_preview=True)

        except Exception as e:
            bot_instance.send_message(chat_id, f"❌ স্ক্র্যাপিং এরর: {str(e)}")
        finally:
            browser.close()

# --- টেলিগ্রাম হ্যান্ডলার ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🚀 **FB Group Scraper (Cookie Version)**\n\nপ্রথমে দেশের নাম দিন (যেমন: USA):")

@bot.message_handler(commands=['export'])
def export_data(message):
    try:
        ref = db.reference('groups')
        data = ref.get()
        if not data:
            bot.send_message(message.chat.id, "ডাটাবেস এখন খালি!")
            return
            
        all_records = []
        for cat in ['auto_approve', 'admin_approve']:
            if cat in data:
                for key in data[cat]:
                    record = data[cat][key]
                    record['Category'] = cat
                    all_records.append(record)
        
        df = pd.DataFrame(all_records)
        df.to_csv("facebook_leads.csv", index=False)
        with open("facebook_leads.csv", 'rb') as f:
            bot.send_document(message.chat.id, f, caption="✅ সকল গ্রুপের ডাটা এক্সপোর্ট করা হয়েছে।")
    except Exception as e:
        bot.send_message(message.chat.id, f"এক্সপোর্ট এরর: {e}")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এখন কি-ওয়ার্ড দিন (যেমন: Marketplace):")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    bot.send_message(chat_id, f"⏳ কাজ শুরু হয়েছে... একটু অপেক্ষা করুন।")
    
    # আলাদা থ্রেডে রান করা যাতে বট হ্যাং না হয়
    threading.Thread(target=scrape_facebook, args=(keyword, country, chat_id, bot)).start()
    del user_states[chat_id]

if __name__ == "__main__":
    # ওয়েব সার্ভার ব্যাকগ্রাউন্ডে চালানো
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot is starting with Cookie Support...")
    bot.infinity_polling()
