import os
import json
import re
import xml.etree.ElementTree as ET
from urllib import request
from datetime import datetime
import sys
import time
import threading
from flask import Flask, render_template_string
import schedule  # Новая библиотека для планировщика

# ===== Конфигурация =====
TOKEN = os.environ.get('TOKEN')
API_KEY = os.environ.get('API_KEY')
CHAT_ID = os.environ.get('CHAT_ID')

if not TOKEN or not API_KEY:
    print("❌ ERROR: TOKEN and API_KEY must be set in environment variables")
    sys.exit(1)

# ===== Flask приложение для Render =====
app = Flask(__name__)

# ===== RSS источники =====
RSS_FEEDS = [
    'https://lenta.ru/rss/news',
    'https://www.vedomosti.ru/rss/news.xml',
    'https://russian.rt.com/rss',
    'http://www.kommersant.ru/RSS/news.xml',
    'https://www.mk.ru/rss/news/index.xml',
    'https://life.ru/rss',
    'https://www.5-tv.ru/news/rss/',
    'http://news.mail.ru/rss/',
    'http://vz.ru/rss.xml',
    'http://www.ria.ru/export/rss2/index.xml'
]

# ===== Функции бота =====
def send_telegram_message(chat_id, text, parse_mode='HTML'):
    """Отправка сообщения через urllib"""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        
        if len(text) > 4000:
            parts = []
            current = ""
            for line in text.split('\n'):
                if len(current) + len(line) + 1 > 3900:
                    parts.append(current)
                    current = line
                else:
                    current += '\n' + line if current else line
            if current:
                parts.append(current)
        else:
            parts = [text]
        
        for part in parts:
            data = json.dumps({
                'chat_id': chat_id,
                'text': part,
                'disable_web_page_preview': True,
                'parse_mode': parse_mode
            }).encode('utf-8')
            
            req = request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            with request.urlopen(req, timeout=10) as resp:
                print(f"Message sent to {chat_id}, status: {resp.status}")
    except Exception as e:
        print(f"Send message error: {e}")

def fetch_url(url, timeout=10):
    """Получение URL через urllib"""
    try:
        req = request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        print(f"Fetch error {url}: {e}")
        return None

def parse_rss(xml_url):
    """Парсинг RSS ленты"""
    items = []
    try:
        print(f"Fetching: {xml_url}")
        content = fetch_url(xml_url)
        if not content:
            print(f"⚠️ No content from {xml_url}")
            return items
        
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            print(f"❌ XML Parse error {xml_url}: {e}")
            return items
        
        def find_in_tree(tag, element=None):
            if element is None:
                element = root
            results = []
            for child in element.iter():
                local_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if local_name == tag:
                    results.append(child)
            return results
        
        for item in find_in_tree('item'):
            title = ''
            desc = ''
            link = ''
            
            for child in item:
                local_name = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                
                if local_name == 'title' and child.text:
                    title = child.text.strip()
                elif local_name in ['description', 'summary'] and child.text:
                    desc = re.sub(r'<[^>]*>', '', child.text).strip()[:300]
                elif local_name == 'link':
                    if child.text and child.text.strip():
                        link = child.text.strip()
                    elif child.get('href'):
                        link = child.get('href')
            
            if title and ('губернатор' in title.lower() or 'губернатор' in desc.lower() or 
                         'врио' in title.lower() or 'врио' in desc.lower() or
                         'отставк' in title.lower() or 'отставк' in desc.lower() or
                         'назначен' in title.lower() or 'назначен' in desc.lower()):
                items.append({'title': title, 'desc': desc, 'link': link})
                print(f"  ✓ Found: {title[:100]}")
        
        print(f"Parsed {len(items)} relevant items from {xml_url}")
    except Exception as e:
        print(f"RSS Error {xml_url}: {str(e)[:150]}")
    
    return items

def process_ai_response(response):
    """Пост-обработка ответа AI"""
    if response == 'нет':
        return 'нет'

    if '|' in response:
        filtered_parts = []
        reject_patterns = [
            'встретил', 'пообщалс', 'провёл', 'провел', 'заявил', 'сообщил',
            'рассказал', 'посетил', 'открыл', 'осмотрел', 'выступил',
            'поздравил', 'вручил', 'прокомментировал', 'атака', 'бпла',
            'пожар', 'наводнение', 'футбол', 'тренер', 'больница', 'операц'
        ]
        accept_keywords = [
            'отставк', 'назначен', 'покинул', 'сложил', 'вступил', 'возглавил',
            'освобождён', 'освобожден', 'отстранён', 'отстранен', 'скончался',
            'умер', 'переизбран', 'переназначен', 'избран', 'утверждён',
            'утвержден', 'сменил', 'переведён', 'переведен', 'заявление', 'прошение'
        ]
        for part in response.split(';'):
            part_lower = part.lower()
            has_accept = any(word in part_lower for word in accept_keywords)
            has_reject = any(word in part_lower for word in reject_patterns)
            if has_accept and not has_reject:
                filtered_parts.append(part.strip())
        if filtered_parts:
            return ' ; '.join(filtered_parts)
        else:
            return 'нет'
    else:
        return response

def call_ai(news_items):
    """Вызов OpenRouter с перебором прокси"""
    print("🔵 [call_ai] START")
    
    if not news_items:
        print("❌ No news items to analyze")
        return ""

    print(f"📊 Analyzing {len(news_items)} news items with AI...")
    
    # --- Формируем промпт ---
    try:
        print("🔵 Формирую промпт...")
        news_text = ""
        for i, item in enumerate(news_items[:15]):
            news_text += f"Новость {i+1}: {item['title']}. {item['desc'][:200]} (ссылка: {item['link']})\n"
        
        prompt = (
            "Ты строгий фильтр новостей. Оставь ТОЛЬКО новости о КАДРОВЫХ ИЗМЕНЕНИЯХ губернаторов РФ.\n\n"
            "⚠️ КАДРОВОЕ ИЗМЕНЕНИЕ — это когда губернатор:\n"
            "- подал в отставку / покинул пост / сложил полномочия\n"
            "- был назначен / утверждён / вступил в должность\n"
            "- был освобождён от должности / отстранён\n"
            "- переизбран / переназначен на новый срок\n"
            "- скончался / умер\n"
            "- переведён на другую должность (полпред, министр, посол)\n"
            "- написал заявление об отставке\n"
            "- объявил о досрочном сложении полномочий\n\n"
            "❌ НЕ ЯВЛЯЮТСЯ кадровыми изменениями (ИГНОРИРУЙ такие новости):\n"
            "- встреча / разговор / переговоры Путина с губернатором\n"
            "- губернатор пообщался / провёл совещание / сделал заявление\n"
            "- губернатор посетил / открыл / осмотрел / выступил\n"
            "- губернатор сообщил / рассказал / прокомментировал\n"
            "- губернатор поздравил / вручил награды\n"
            "- любые новости не о губернаторах (футбол, больницы, экономика)\n"
            "- новости о вице-губернаторах, министрах, депутатах, мэрах\n\n"
            "Правила отбора:\n"
            "1. В новости ДОЛЖНО быть явное указание на СМЕНУ статуса губернатора\n"
            "2. Слова 'назначен', 'отставка', 'освобождён', 'вступил' — пропускаем\n"
            "3. Слова 'встретился', 'пообщался', 'провёл', 'заявил' — отклоняем\n"
            "4. Если сомневаешься — НЕ ВКЛЮЧАЙ\n\n"
            "Для каждой отобранной новости укажи:\n"
            "Регион | событие (с ФИО и должностью) | ссылка\n\n"
            "Примеры ПРАВИЛЬНЫХ ответов:\n"
            "Саратовская область | губернатор Роман Бусаргин подал в отставку | https://example.com\n"
            "Архангельская область | Александр Цыбульский назначен врио губернатора | https://example.com\n\n"
            "Если НЕТ ни одной новости о кадровых изменениях — напиши 'нет'\n\n"
            "Разделяй новости через ;\n\n"
            f"{news_text}"
        )
        print(f"✅ Промпт сформирован, длина: {len(prompt)} символов")
    except Exception as e:
        print(f"❌ Ошибка при формировании промпта: {e}")
        import traceback
        traceback.print_exc()
        return "нет"

    # --- Пробуем прямой доступ ---
    try:
        print("🌐 [1] Trying direct access to OpenRouter...")
        
        data = json.dumps({
            "model": "google/gemma-4-26b-a4b-it:free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 800,
            "temperature": 0
        }).encode('utf-8')
        print(f"✅ JSON сформирован, размер: {len(data)} байт")
        
        req = request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            }
        )
        print("⏳ Отправляю запрос к OpenRouter (таймаут 45 сек)...")
        
        with request.urlopen(req, timeout=45) as resp:
            print(f"✅ Получен ответ, статус: {resp.status}")
            result = json.loads(resp.read().decode('utf-8'))
            print("✅ JSON ответа распарсен")
            
            if result and "choices" in result:
                response = result["choices"][0]["message"]["content"].strip()
                print(f"📝 AI ответ: {response[:100]}...")
                return process_ai_response(response)
            else:
                print("❌ Нет choices в ответе OpenRouter")
                print(f"   Ответ: {str(result)[:200]}")
                
    except Exception as e:
        error_msg = str(e)
        print(f"❌ [1] Direct access failed: {error_msg}")
        
        # Если ошибка 403 — пробуем прокси
        if "403" in error_msg:
            print("🔄 [2] 403 Forbidden, пробую прокси...")
            user_proxy = os.environ.get('PROXY_URL', '').strip()
            if user_proxy:
                try:
                    print(f"🌐 [3] Trying proxy: {user_proxy}")
                    
                    data = json.dumps({
                        "model": "google/gemma-4-26b-a4b-it:free",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 800,
                        "temperature": 0
                    }).encode('utf-8')
                    
                    proxy_handler = request.ProxyHandler({
                        'https': user_proxy,
                        'http': user_proxy
                    })
                    opener = request.build_opener(proxy_handler)
                    req = request.Request(
                        "https://openrouter.ai/api/v1/chat/completions",
                        data=data,
                        headers={
                            "Authorization": f"Bearer {API_KEY}",
                            "Content-Type": "application/json"
                        }
                    )
                    print("⏳ Отправляю через прокси...")
                    with opener.open(req, timeout=45) as resp:
                        print(f"✅ Получен ответ через прокси, статус: {resp.status}")
                        result = json.loads(resp.read().decode('utf-8'))
                        if result and "choices" in result:
                            response = result["choices"][0]["message"]["content"].strip()
                            print(f"✅ Success with proxy: {user_proxy}")
                            return process_ai_response(response)
                except Exception as proxy_error:
                    print(f"❌ [3] Proxy failed: {proxy_error}")
            else:
                print("❌ [2] PROXY_URL не задан в переменных окружения")
        else:
            print(f"❌ [2] Другая ошибка, прокси не пробую")

    print("❌ Все методы подключения не сработали")
    return "нет"

def format_news_beautiful(ai_response, news_items):
    """Красивое форматирование новостей"""
    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    
    header = (
        f"📰 <b>СВОДКА КАДРОВЫХ ИЗМЕНЕНИЙ ГУБЕРНАТОРОВ</b>\n"
        f"📅 {date_str}\n"
        f"{'—' * 15}\n\n"
    )

    header_nonews = (
        f"📰 <b>❌ Новостей о кадровых изменениях губернаторов не найдено</b>\n"
        f"{'—' * 15}\n\n"
        f"📅 {date_str}\n"
    )
    
    if ai_response and ai_response != 'нет' and '|' in ai_response:
        parts = [p.strip() for p in ai_response.split(';') if p.strip() and '|' in p]
        news_blocks = []
        
        for p in parts[:10]:
            chunks = p.split('|')
            if len(chunks) >= 3:
                region = chunks[0].strip()
                event = chunks[1].strip()
                link = chunks[2].strip()
                
                event_lower = event.lower()
                if any(w in event_lower for w in ['отставк', 'покинул', 'сложил', 'ушел', 'ушёл']):
                    emoji = "🔄"
                elif any(w in event_lower for w in ['назначен', 'вступил', 'возглавил', 'утверждён', 'утвержден']):
                    emoji = "✅"
                elif any(w in event_lower for w in ['скончался', 'смерть', 'умер']):
                    emoji = "🕯"
                elif any(w in event_lower for w in ['отстранён', 'отстранен']):
                    emoji = "⚠️"
                else:
                    emoji = "📌"
                
                block = (
                    f"{emoji} <b>Регион:</b> {region}\n"
                    f"   <b>Событие:</b> {event}\n"
                    f"   <b>Ссылка:</b> {link}\n"
                    f"{'—' * 15}"
                )
                news_blocks.append(block)
        
        if news_blocks:
            return header + "\n\n".join(news_blocks)
    
    return header_nonews

def run_analysis():
    """Основная функция анализа"""
    print("=== Starting analysis ===")
    
    all_news = []
    success_sources = 0
    
    for feed in RSS_FEEDS:
        try:
            items = parse_rss(feed)
            if items:
                success_sources += 1
            all_news.extend(items)
        except Exception as e:
            print(f"❌ Source {feed} crashed: {e}")
            continue
    
    print(f"Total relevant news: {len(all_news)} from {success_sources} sources")
    
    if not all_news:
        return "❌ Нет новостей о кадровых изменениях губернаторов"
    
    unique = {}
    for item in all_news:
        key = item['title'][:100]
        if key not in unique:
            unique[key] = item
    
    filtered = list(unique.values())
    print(f"After dedup: {len(filtered)}")
    
    print("🤖 Calling AI...")
    ai_result = call_ai(filtered[:15])
    print(f"🤖 AI result: {ai_result[:100] if ai_result else 'empty'}")
    
    result = format_news_beautiful(ai_result, filtered)
    return result

def send_daily_report():
    """Отправляет ежедневную сводку в группу"""
    print(f"📅 Running daily report at {datetime.now()}")
    try:
        result = run_analysis()
        result = result.replace(
            "📰 <b>СВОДКА КАДРОВЫХ ИЗМЕНЕНИЙ ГУБЕРНАТОРОВ</b>",
            "🌅 <b>ЕЖЕДНЕВНАЯ СВОДКА КАДРОВЫХ ИЗМЕНЕНИЙ ГУБЕРНАТОРОВ</b>"
        )
        if CHAT_ID:
            send_telegram_message(CHAT_ID, result)
            print("✅ Daily report sent!")
        else:
            print("❌ CHAT_ID not set")
    except Exception as e:
        print(f"❌ Daily report error: {e}")

# ===== ПЛАНИРОВЩИК =====
def scheduled_report():
    """Функция для планировщика"""
    print(f"⏰ Scheduled report triggered at {datetime.now()}")
    send_daily_report()

def start_scheduler():
    """Запускает планировщик в отдельном потоке"""
    # 07:15 UTC = 10:15 MSK (если сервер в UTC)
    schedule.every().day.at("08:21").do(scheduled_report)
    print("⏰ Scheduler started. Daily report set for 07:15 UTC (10:15 MSK)")
    
    while True:
        schedule.run_pending()
        time.sleep(30)  # Проверять каждые 30 секунд

# ===== Обработка команд =====
def get_updates(offset=None):
    """Получение обновлений от Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        if offset:
            url += f"?offset={offset}"
        req = request.Request(url)
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"Get updates error: {e}")
        return None

def process_updates():
    """Обработка команд в личных сообщениях"""
    last_update_id = 0
    
    print("🤖 Bot started. Listening for commands...")
    print("Commands in private chat: /start, /analyse, /test, /chatid")
    
    while True:
        try:
            updates = get_updates(last_update_id + 1)
            if updates and 'result' in updates:
                for update in updates['result']:
                    last_update_id = update['update_id']
                    
                    if 'message' in update:
                        msg = update['message']
                        chat_id = msg['chat']['id']
                        chat_type = msg['chat'].get('type', 'private')
                        text = msg.get('text', '')
                        
                        if chat_type == 'private':
                            print(f"📩 Private message: {text} from {chat_id}")
                            
                            if text == '/start':
                                send_telegram_message(chat_id, 
                                    "👋 <b>Бот для отслеживания кадровых изменений губернаторов РФ</b>\n\n"
                                    "📡 Мониторинг 12 источников СМИ\n"
                                    "🤖 AI-анализ новостей\n"
                                    "📊 Ежедневные сводки\n\n"
                                    "<b>Команды:</b>\n"
                                    "/analyse — получить сводку новостей\n"
                                    "/test — проверить работу бота\n"
                                    "/chatid — показать ID чата"
                                )
                            elif text == '/test':
                                send_telegram_message(chat_id, "✅ Бот работает!")
                            elif text == '/analyse':
                                send_telegram_message(chat_id, "⏳ <b>Анализирую новости из 12 источников...</b>")
                                try:
                                    result = run_analysis()
                                    send_telegram_message(chat_id, result)
                                except Exception as e:
                                    error_msg = f"❌ Ошибка: {str(e)}"
                                    send_telegram_message(chat_id, error_msg)
                                    print(f"❌ Analysis error: {e}")
                            elif text == '/chatid':
                                send_telegram_message(chat_id, f"ID чата: <code>{chat_id}</code>")
                            else:
                                send_telegram_message(chat_id, 
                                    "❌ Неизвестная команда.\n"
                                    "Доступные команды:\n"
                                    "/start — приветствие\n"
                                    "/analyse — получить сводку\n"
                                    "/test — проверить работу\n"
                                    "/chatid — показать ID чата"
                                )
                        else:
                            print(f"📢 Group message (ignored): {text[:50]} from {chat_id}")
            
            time.sleep(2)
            
        except Exception as e:
            print(f"Process error: {e}")
            time.sleep(5)

def start_bot():
    """Запускает бота в отдельном потоке"""
    try:
        process_updates()
    except Exception as e:
        print(f"Bot thread error: {e}")

# ===== Flask веб-сервер для Render =====
@app.route('/')
def index():
    """Страница-заглушка чтобы Render видел открытый порт"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Governor News Bot</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                background: #f0f0f0;
            }
            .container {
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 { color: #2c3e50; }
            .status { color: #27ae60; font-weight: bold; }
            .commands {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                font-family: monospace;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Governor News Bot</h1>
            <p>Status: <span class="status">✅ Running</span></p>
            <p>Бот активен и слушает команды в Telegram.</p>
            <p>📅 Ежедневные отчёты отправляются в 10:15 MSK</p>
            <h3>Доступные команды:</h3>
            <div class="commands">
                /start — приветствие<br>
                /analyse — получить сводку новостей<br>
                /test — проверить работу<br>
                /chatid — показать ID чата
            </div>
            <p style="margin-top: 20px; color: #7f8c8d; font-size: 14px;">
                Последний пинг: {{ now }}
            </p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/ping')
def ping():
    """Эндпоинт для keep-alive пинга"""
    return "OK", 200

# ===== ГЛАВНАЯ ФУНКЦИЯ =====
if __name__ == "__main__":
    # Проверяем аргументы командной строки
    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        # Режим ежедневной рассылки (для ручного запуска)
        send_daily_report()
    else:
        # Запускаем планировщик в отдельном потоке
        scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
        scheduler_thread.start()
        print("✅ Scheduler thread started")
        
        # Запускаем бота в отдельном потоке
        bot_thread = threading.Thread(target=start_bot, daemon=True)
        bot_thread.start()
        print("✅ Bot thread started")
        
        # Запускаем Flask веб-сервер
        port = int(os.environ.get("PORT", 10000))
        print(f"🌐 Starting web server on port {port}")
        app.run(host='0.0.0.0', port=port)
