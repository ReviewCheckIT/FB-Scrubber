import os
import time
import random
import json
import telebot
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv

# এনভায়রনমেন্ট ভেরিয়েবল লোড করা
load_dotenv()

# --- কনফিগারেশন (Render Environment Variables থেকে আসবে) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
FB_EMAIL = os.getenv("FB_EMAIL")
FB_PASSWORD = os.getenv("FB_PASSWORD")
FIREBASE_JSON = os.getenv("FIREBASE_CREDENTIALS")
DB_URL = os.getenv("DB_URL")

# --- ফায়ারবেস ইনিশিয়ালাইজেশন ---
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': DB_URL})
    except Exception as e:
        print(f"Firebase Init Error: {e}")

def save_to_firebase(group_data):
    try:
        ref = db.reference('groups')
        # লিংকের স্পেশাল ক্যারেক্টার ক্লিন করা ডাটাবেস কি-এর জন্য
        safe_key = group_data['link'].replace('.', '_').replace('/', '|').replace(':', '')
        
        if not ref.child(safe_key).get():
            ref.child(safe_key).set(group_data)
            return True
        return False
    except Exception as e:
        print(f"Database Error: {e}")
        return False

# --- স্ক্র্যাপিং ফাংশন ---
def scrape_facebook(keyword, country):
    results = []
    with sync_playwright() as p:
        # Render-এর জন্য হেডলেস মোড এবং নো-স্যান্ডবক্স
        browser = p.chromium.launch(headless=True, args=["--disable-notifications", "--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # ফেসবুক লগইন
            page.goto("https://www.facebook.com/login", wait_until="networkidle")
            page.fill("input[name='email']", FB_EMAIL)
            page.fill("input[name='pass']", FB_PASSWORD)
            page.click("button[name='login']")
            time.sleep(5) # লগইন হওয়ার জন্য সময় দেওয়া

            # সার্চ ইউআরএল
            search_url = f"https://www.facebook.com/search/groups/?q={keyword}"
            page.goto(search_url, wait_until="networkidle")
            time.sleep(random.uniform(4, 6))

            # উন্নত স্ক্রলিং লজিক (মানুষের মতো আচরণ)
            for i in range(5):
                scroll_distance = random.randint(700, 1200)
                page.mouse.wheel(0, scroll_distance)
                print(f"Scrolling {i+1}...")
                time.sleep(random.uniform(3, 6))

            # ডাটা এক্সট্রাকশন (পাবলিক গ্রুপ ফিল্টার)
            # ফেসবুকের বর্তমান স্ট্রাকচার অনুযায়ী এংকর ট্যাগ খোঁজা
            group_links = page.locator("a[href*='/groups/']").all()
            
            seen_links = set()
            for link_loc in group_links:
                try:
                    href = link_loc.get_attribute("href")
                    if href and "/groups/" in href:
                        clean_link = href.split('?')[0].rstrip('/')
                        if clean_link not in seen_links:
                            name = link_loc.inner_text().split('\n')[0]
                            if name:
                                data = {
                                    "name": name,
                                    "link": clean_link,
                                    "keyword": keyword,
                                    "country": country,
                                    "found_at": time.strftime("%Y-%m-%d %H:%M:%S")
                                }
                                results.append(data)
                                seen_links.add(clean_link)
                except:
                    continue
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
    
    bot.send_message(chat_id, f"🔍 {country}-তে '{keyword}' সম্পর্কিত পাবলিক গ্রুপ খোঁজা হচ্ছে। এটি কয়েক মিনিট সময় নিতে পারে...")
    
    try:
        found_groups = scrape_facebook(keyword, country)
        new_count = 0
        
        if found_groups:
            for g in found_groups:
                if save_to_firebase(g):
                    new_count += 1
                    bot.send_message(chat_id, f"📌 **{g['name']}**\n🔗 {g['link']}", parse_mode="Markdown", disable_web_page_preview=True)
            
            if new_count > 0:
                bot.send_message(chat_id, f"✅ কাজ শেষ! মোট {new_count}টি নতুন গ্রুপ ডাটাবেসে সেভ হয়েছে।")
            else:
                bot.send_message(chat_id, "নতুন কোনো ইউনিক গ্রুপ পাওয়া যায়নি (সবগুলো ডাটাবেসে আগে থেকেই আছে)।")
        else:
            bot.send_message(chat_id, "দুঃখিত, কোনো গ্রুপ খুঁজে পাওয়া যায়নি। আপনার কিওয়ার্ড পরিবর্তন করে চেষ্টা করুন।")
            
    except Exception as e:
        bot.send_message(chat_id, f"❌ একটি সমস্যা হয়েছে: {str(e)}")
    
    # ইউজার স্টেট ক্লিয়ার করা যাতে নতুন সার্চ করা যায়
    if chat_id in user_states:
        del user_states[chat_id]

if __name__ == "__main__":
    print("Bot is starting...")
    bot.infinity_polling()
