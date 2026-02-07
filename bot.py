import os
import time
import logging
import json
import sqlite3
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from tenacity import retry, stop_after_attempt, wait_exponential

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from telebot import TeleBot

# ============ BOT TOKEN ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

bot = TeleBot(TELEGRAM_BOT_TOKEN)

# ============ LOGGING AYARLARI ============
class LoggerSetup:
    @staticmethod
    def setup_logger(base_path="/tmp/tiktok_logs"):
        log_dir = base_path
        os.makedirs(log_dir, exist_ok=True)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(os.path.join(log_dir, f"download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")),
                logging.StreamHandler()
            ]
        )
        return logging.getLogger(__name__)

# ============ KONFİGÜRASYON YÖNETICISI ============
class ConfigManager:
    def __init__(self, config_file="/tmp/tiktok_config.json"):
        self.config_file = config_file
        self.load_config()
    
    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
        else:
            self.config = self.get_default_config()
            self.save_config()
    
    def get_default_config(self):
        return {
            "download_path": "/tmp/tiktok_downloads",
            "delay_between_downloads": 3,
            "timeout": 25,
            "max_workers": 2,
            "use_proxy": False,
            "proxy_server": "",
            "enable_logging": True,
            "scrape_scroll_count": 5,
            "headless_mode": True
        }
    
    def save_config(self):
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=4)
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
        self.save_config()

# ============ VERİTABANI YÖNETICISI ============
class DatabaseManager:
    def __init__(self, base_path="/tmp/tiktok_db"):
        os.makedirs(base_path, exist_ok=True)
        self.db_path = os.path.join(base_path, "downloads.db")
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY,
                video_id TEXT UNIQUE,
                username TEXT,
                url TEXT,
                status TEXT,
                download_date TIMESTAMP,
                file_path TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telegram_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                downloads_count INTEGER DEFAULT 0,
                join_date TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    
    def mark_as_downloaded(self, video_id, username, url, status, file_path=""):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO downloads 
                (video_id, username, url, status, download_date, file_path)
                VALUES (?, ?, ?, ?, datetime('now'), ?)
            ''', (video_id, username, url, status, file_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database error: {e}")
    
    def is_already_downloaded(self, video_id):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM downloads WHERE video_id = ? AND status = "success"', (video_id,))
            result = cursor.fetchone()
            conn.close()
            return result is not None
        except:
            return False
    
    def get_download_stats(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "success"')
            success = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM downloads WHERE status = "failed"')
            failed = cursor.fetchone()[0]
            conn.close()
            return success, failed
        except:
            return 0, 0
    
    def add_telegram_user(self, user_id, username):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO telegram_users (user_id, username, join_date)
                VALUES (?, ?, datetime('now'))
            ''', (user_id, username))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding telegram user: {e}")

# ============ CHROME YÖNETICISI (HEADLESS OPTIMIZED) ============
class ChromeManager:
    @staticmethod
    def create_driver(config):
        options = uc.ChromeOptions()
        
        # Choreo ve Sunucu Ortamları İçin Kritik Ayarlar
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-extensions")
        
        if config.get("use_proxy") and config.get("proxy_server"):
            options.add_argument(f"--proxy-server={config.get('proxy_server')}")
        
        try:
            driver = uc.Chrome(
                options=options,
                use_subprocess=True,
                headless=True
            )
        except Exception as e:
            logger.error(f"Chrome başlatma hatası (Binary hatası olabilir): {e}")
            raise
        
        driver.set_page_load_timeout(config.get("timeout", 25))
        return driver

# ============ İNDİRME MOTORU ============
class TikTokDownloader:
    def __init__(self, config_manager, db_manager):
        self.config_manager = config_manager
        self.db_manager = db_manager
        self.base_path = config_manager.get("download_path")
        os.makedirs(self.base_path, exist_ok=True)
    
    def send_telegram_message(self, chat_id, message):
        try:
            bot.send_message(chat_id, message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Telegram message error: {e}")
    
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
    def download_single_video(self, driver, link, save_dir, video_id, is_photo, username):
        try:
            before_count = len(os.listdir(save_dir)) if os.path.exists(save_dir) else 0
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": save_dir})

            if is_photo:
                driver.get("https://imaiger.com/tool/tiktok-slideshow-downloader")
                time.sleep(6)
                wait = WebDriverWait(driver, self.config_manager.get("timeout", 25))
                p_in = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input")))
                driver.execute_script("arguments[0].value = ''; arguments[0].focus();", p_in)
                for char in link:
                    p_in.send_keys(char)
                    time.sleep(0.01)
                try:
                    driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[contains(., 'Load')]"))
                except:
                    p_in.send_keys(Keys.ENTER)
                time.sleep(5)
                driver.execute_script("arguments[0].click();", wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Download All')]"))))
                time.sleep(5)
            else:
                driver.get("https://www.tikwm.com/originalDownloader.html")
                wait = WebDriverWait(driver, self.config_manager.get("timeout", 25))
                input_f = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input#url, .form-control")))
                js_script = "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true })); arguments[0].dispatchEvent(new Event('change', { bubbles: true }));"
                driver.execute_script(js_script, input_f, link)
                time.sleep(2)
                driver.execute_script("arguments[0].click();", wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#search_btn"))))
                dl_btn = wait.until(EC.presence_of_element_located((By.XPATH, "//a[contains(@class, 'download') and contains(@href, 'tikwm.com')]")))
                driver.execute_script("arguments[0].click();", dl_btn)
                time.sleep(6)

            if os.path.exists(save_dir) and len(os.listdir(save_dir)) > before_count:
                self.db_manager.mark_as_downloaded(video_id, username, link, "success")
                logger.info(f"İndirildi: {link}")
                return True
            else:
                raise Exception("Dosya indirilmedi")
        except Exception as e:
            logger.error(f"Download error: {e}")
            raise
    
    def scrape_user(self, username):
        driver = None
        try:
            driver = ChromeManager.create_driver(self.config_manager)
            if not username.startswith("@"):
                username = "@" + username
            
            driver.get(f"https://www.tiktok.com/{username}")
            time.sleep(6)
            
            found_links = set()
            scroll_count = self.config_manager.get("scrape_scroll_count", 5)
            
            for _ in range(scroll_count):
                elements = driver.find_elements(By.XPATH, "//a[contains(@href, '/video/') or contains(@href, '/photo/')]")
                for el in elements:
                    href = el.get_attribute("href")
                    if href:
                        found_links.add(href.split("?")[0])
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(3)
            
            logger.info(f"Scrape başarılı: {len(found_links)} link bulundu")
            return list(found_links)
        except Exception as e:
            logger.error(f"Scrape hatası: {e}")
            return []
        finally:
            if driver:
                driver.quit()
    
    def download_videos(self, links, chat_id=None):
        total = len(links)
        success_count = 0
        fail_count = 0
        failed_links = []
        drivers = [] # UnboundLocalError önlemek için liste en başta tanımlanmalı
        
        if chat_id:
            self.send_telegram_message(chat_id, f"⏳ <b>{total}</b> video indirme başlatılıyor...")
        
        try:
            max_workers = self.config_manager.get("max_workers", 1)
            # Sürücüleri oluştur
            drivers = [ChromeManager.create_driver(self.config_manager) for _ in range(max_workers)]
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                
                for index, link in enumerate(links):
                    driver = drivers[index % max_workers]
                    
                    video_id = link.split('/')[-1].split('?')[0]
                    is_photo = "/photo/" in link
                    username = link.split('@')[1].split('/')[0] if '@' in link else "user"
                    save_dir = os.path.join(self.base_path, username)
                    os.makedirs(save_dir, exist_ok=True)

                    if self.db_manager.is_already_downloaded(video_id):
                        success_count += 1
                        continue

                    future = executor.submit(
                        self.download_single_video,
                        driver, link, save_dir, video_id, is_photo, username
                    )
                    futures[future] = (link, username)
                    
                    time.sleep(self.config_manager.get("delay_between_downloads", 3))

                for future in as_completed(futures):
                    link, username = futures[future]
                    try:
                        future.result()
                        success_count += 1
                        if chat_id and success_count % 5 == 0:
                            self.send_telegram_message(chat_id, f"✅ {success_count}/{total} indirildi...")
                    except Exception as e:
                        fail_count += 1
                        failed_links.append(link)
                        video_id = link.split('/')[-1].split('?')[0]
                        self.db_manager.mark_as_downloaded(video_id, username, link, "failed")
                        logger.error(f"Failed: {link}")
        finally:
            # Sürücüleri güvenli kapat
            for d in drivers:
                try:
                    d.quit()
                except:
                    pass
            
            if chat_id:
                telegram_msg = f"✅ <b>İndirme Tamamlandı!</b>\n\n📊 <b>Sonuçlar:</b>\n✅ Başarılı: <b>{success_count}</b>\n❌ Hatalı: <b>{fail_count}</b>"
                self.send_telegram_message(chat_id, telegram_msg)
            
            return success_count, fail_count, failed_links

# ============ TELEGRAM HANDLERS ============
@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    username = message.from_user.username or message.from_user.first_name or "User"
    db_manager.add_telegram_user(chat_id, username)
    
    response = "🎬 <b>TikTok Pro Downloader Bot</b>\n\nHoşgeldin! 👋\n\n📌 <b>Komutlar:</b>\n/download - Video/Foto indirmek için\n/scrape - Kullanıcıdan videoları çekmek için\n/stats - İstatistikleri görmek için\n/help - Yardım almak için"
    bot.send_message(chat_id, response, parse_mode='HTML')

@bot.message_handler(commands=['download'])
def handle_download(message):
    chat_id = message.chat.id
    msg = bot.send_message(chat_id, "🔗 <b>Lütfen TikTok linkini gönder:</b>", parse_mode='HTML')
    bot.register_next_step_handler(msg, process_download_link, chat_id)

@bot.message_handler(commands=['scrape'])
def handle_scrape(message):
    chat_id = message.chat.id
    msg = bot.send_message(chat_id, "👤 <b>Lütfen TikTok kullanıcı adını gönder:</b>", parse_mode='HTML')
    bot.register_next_step_handler(msg, process_scrape_user, chat_id)

@bot.message_handler(commands=['stats'])
def handle_stats(message):
    chat_id = message.chat.id
    success, failed = db_manager.get_download_stats()
    stats_text = f"📊 <b>İstatistikler:</b>\n\n✅ Başarılı İndirmeler: <b>{success}</b>\n❌ Hatalı İndirmeler: <b>{failed}</b>\n📈 Toplam: <b>{success + failed}</b>"
    bot.send_message(chat_id, stats_text, parse_mode='HTML')

@bot.message_handler(func=lambda message: "tiktok.com" in message.text.lower())
def handle_tiktok_link(message):
    process_download_link(message, message.chat.id)

def process_download_link(message, chat_id):
    link = message.text.strip()
    if "tiktok.com" not in link:
        bot.send_message(chat_id, "❌ <b>Geçerli bir TikTok linki gönder!</b>", parse_mode='HTML')
        return
    
    threading.Thread(target=downloader.download_videos, args=([link], chat_id), daemon=True).start()

def process_scrape_user(message, chat_id):
    username = message.text.strip()
    if not username: return
    
    def run_scrape():
        bot.send_message(chat_id, f"⏳ <b>{username}</b> videoları toplanıyor...", parse_mode='HTML')
        links = downloader.scrape_user(username)
        if links:
            bot.send_message(chat_id, f"✅ <b>{len(links)}</b> video bulundu, indirme başlıyor...", parse_mode='HTML')
            downloader.download_videos(links, chat_id)
        else:
            bot.send_message(chat_id, "❌ Video bulunamadı.", parse_mode='HTML')
            
    threading.Thread(target=run_scrape, daemon=True).start()

# ============ START ============
if __name__ == "__main__":
    logger = LoggerSetup.setup_logger()
    config_manager = ConfigManager()
    db_manager = DatabaseManager()
    downloader = TikTokDownloader(config_manager, db_manager)
    
    print("\n✅ Bot Choreo üzerinde başlatıldı!\n")
    bot.infinity_polling()
