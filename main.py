import os
import time
import random
import json
import telebot
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা (Render এর জন্য)
load_dotenv()

# --- কনফিগারেশন ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FB_COOKIES = os.getenv("FB_COOKIES") # Optional
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

# --- ফায়ারবেস ইনিশিয়ালাইজেশন ---
if FIREBASE_JSON:
    cred_dict = json.loads(FIREBASE_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})

def save_to_firebase(group_data):
    try:
        ref = db.reference('groups')
        safe_key = group_data['link'].replace('.', '_').replace('/', '|')
        if not ref.child(safe_key).get():
            ref.child(safe_key).set(group_data)
            return True
        return False
    except Exception as e:
        print(f"DB Error: {e}")
        return False

# --- স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        # Render এর জন্য headless: True এবং প্রয়োজনীয় ডেরাইভালস
        browser = p.chromium.launch(headless=True, args=["--disable-notifications", "--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        # ফেইসবুক লগইন
        page.goto("https://www.facebook.com/login")
        page.fill("input[name='email']", FB_EMAIL)
        page.fill("input[name='pass']", FB_PASSWORD)
        page.click("button[name='login']")
        page.wait_for_load_state("networkidle")

        # সার্চ ইউআরএল (পাবলিক গ্রুপ ফিল্টার সহ সার্চ)
        search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
        page.goto(search_url)
        time.sleep(5)

        # স্ক্রলিং লজিক
        for _ in range(3):
            page.mouse.wheel(0, 1000)
            time.sleep(2)

        # ডাটা এক্সট্রাকশন
        links = page.locator("a[href*='/groups/']").all()
        for link_loc in links[:10]: # প্রথম ১০টি গ্রুপ
            try:
                name = link_loc.inner_text()
                link = link_loc.get_attribute("href").split('?')[0]
                
                if name and "/groups/" in link:
                    data = {
                        "name": name,
                        "link": link,
                        "keyword": keyword,
                        "country": country,
                        "timestamp": time.time()
                    }
                    results.append(data)
            except:
                continue
        
        browser.close()
    return results

# --- টেলিগ্রাম বট লজিক ---
bot = telebot.TeleBot(BOT_TOKEN)
user_states = {}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "স্বাগতম! প্রজেক্ট মাস্টারে। কোন দেশের গ্রুপ খুঁজছেন? (যেমন: USA)")

@bot.message_handler(func=lambda m: m.chat.id not in user_states)
def get_country(message):
    user_states[message.chat.id] = {'country': message.text}
    bot.reply_to(message, "এবার আপনার Niche বা Keyword লিখুন (যেমন: Freelancing):")

@bot.message_handler(func=lambda m: len(user_states.get(m.chat.id, {})) == 1)
def get_keyword(message):
    chat_id = message.chat.id
    country = user_states[chat_id]['country']
    keyword = message.text
    
    bot.send_message(chat_id, f"🔍 {country}-তে '{keyword}' এর গ্রুপ খোঁজা হচ্ছে...")
    
    try:
        found_groups = scrape_facebook(keyword, country)
        new_count = 0
        
        for g in found_groups:
            if save_to_firebase(g):
                new_count += 1
                bot.send_message(chat_id, f"📌 **{g['name']}**\n🔗 {g['link']}", parse_mode="Markdown")
        
        if new_count == 0:
            bot.send_message(chat_id, "নতুন কোনো গ্রুপ পাওয়া যায়নি।")
        else:
            bot.send_message(chat_id, f"✅ মোট {new_count}টি নতুন গ্রুপ ডাটাবেসে সেভ হয়েছে।")
            
    except Exception as e:
        bot.send_message(chat_id, f"Error: {str(e)}")
    
    del user_states[chat_id] # স্টেট ক্লিয়ার করা

if __name__ == "__main__":
    print("Bot is running...")
    bot.infinity_polling()
