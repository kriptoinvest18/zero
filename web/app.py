"""
Веб-сайт Магия Камней — работает параллельно с ботом
на том же Railway сервисе через aiohttp.
"""
import os
import json
import logging
import aiohttp
from aiohttp import web
from pathlib import Path
from datetime import datetime
import sqlite3

logger = logging.getLogger(__name__)

# Пути
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / 'templates'
STATIC_DIR = Path(__file__).resolve().parent / 'static'
KB_DIR = BASE_DIR / 'content' / 'knowledge_base'


def get_db_path():
    from src.config import Config
    return Config.DB_PATH


def db_query(sql, params=()):
    """Выполнить запрос к БД."""
    try:
        conn = sqlite3.connect(str(get_db_path()))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(sql, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"DB error: {e}")
        return []


def load_all_stones():
    """Загрузить все камни из файлов."""
    stones = {}
    if not KB_DIR.exists():
        return stones
    for f in sorted(KB_DIR.glob('*.txt')):
        stone_id = f.stem
        data = {}
        current_key = None
        current_lines = []
        for line in f.read_text(encoding='utf-8').split('\n'):
            line = line.strip()
            if line.startswith('[') and line.endswith(']'):
                if current_key:
                    data[current_key] = '\n'.join(current_lines).strip()
                current_key = line[1:-1]
                current_lines = []
            elif current_key:
                current_lines.append(line)
        if current_key:
            data[current_key] = '\n'.join(current_lines).strip()
        if data:
            stones[stone_id] = data
    return stones


def render_template(name, **ctx):
    """Простой рендер шаблона."""
    tmpl_path = TEMPLATES_DIR / name
    with open(tmpl_path, encoding='utf-8') as f:
        content = f.read()
    # Simple variable substitution
    for key, val in ctx.items():
        content = content.replace('{{ ' + key + ' }}', str(val) if val is not None else '')
        content = content.replace('{{' + key + '}}', str(val) if val is not None else '')
    return content


async def handle_static(request):
    """Отдача статических файлов."""
    filename = request.match_info['filename']
    filepath = (STATIC_DIR / filename).resolve()
    static_root = STATIC_DIR.resolve()
    if not filepath.exists() or filepath.is_dir():
        raise web.HTTPNotFound()
    if not str(filepath).startswith(str(static_root)):
        raise web.HTTPNotFound()
    return web.FileResponse(filepath)


async def handle_index(request):
    """Главная страница."""
    # Берём несколько товаров витрины
    products = db_query("""
        SELECT si.id, si.name, si.description, si.price, si.stars_price,
               si.image_file_id, sc.name as collection_name, sc.emoji
        FROM showcase_items si
        JOIN showcase_collections sc ON si.collection_id = sc.id
        ORDER BY si.created_at DESC
        LIMIT 6
    """)

    stones = load_all_stones()
    featured_stones = [
        (sid, d) for sid, d in list(stones.items())[:6]
    ]

    products_html = _render_products_grid(products, limit=6)
    stones_html = _render_stones_grid(featured_stones)

    with open(TEMPLATES_DIR / 'index.html', encoding='utf-8') as f:
        html = f.read()

    html = html.replace('<!-- PRODUCTS_GRID -->', products_html)
    html = html.replace('<!-- STONES_GRID -->', stones_html)
    html = html.replace('<!-- TOTAL_STONES -->', str(len(stones)))
    html = html.replace('<!-- TOTAL_PRODUCTS -->', str(len(products)))

    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_catalog(request):
    """Каталог товаров."""
    collections = db_query("""
        SELECT sc.id, sc.name, sc.emoji, sc.description,
               COUNT(si.id) as items_count
        FROM showcase_collections sc
        LEFT JOIN showcase_items si ON si.collection_id = sc.id
        GROUP BY sc.id
        ORDER BY sc.id
    """)

    col_id = request.rel_url.query.get('col')
    if col_id:
        products = db_query("""
            SELECT si.*, sc.name as collection_name, sc.emoji
            FROM showcase_items si
            JOIN showcase_collections sc ON si.collection_id = sc.id
            WHERE si.collection_id = ?
            ORDER BY si.created_at DESC
        """, (col_id,))
        active_col = int(col_id)
    else:
        products = db_query("""
            SELECT si.*, sc.name as collection_name, sc.emoji
            FROM showcase_items si
            JOIN showcase_collections sc ON si.collection_id = sc.id
            ORDER BY si.created_at DESC
        """)
        active_col = None

    products_html = _render_products_grid(products)
    collections_html = _render_collections_tabs(collections, active_col)

    with open(TEMPLATES_DIR / 'catalog.html', encoding='utf-8') as f:
        html = f.read()

    html = html.replace('<!-- COLLECTIONS_TABS -->', collections_html)
    html = html.replace('<!-- PRODUCTS_GRID -->', products_html)
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_stones(request):
    """База знаний о камнях."""
    stones = load_all_stones()
    search = request.rel_url.query.get('q', '').lower().strip()

    if search:
        filtered = {
            sid: d for sid, d in stones.items()
            if search in d.get('TITLE', '').lower()
            or search in d.get('PROPERTIES', '').lower()
            or search in d.get('SHORT_DESC', '').lower()
        }
    else:
        filtered = stones

    stones_html = _render_stones_list(list(filtered.items()))

    with open(TEMPLATES_DIR / 'stones.html', encoding='utf-8') as f:
        html = f.read()

    html = html.replace('<!-- STONES_LIST -->', stones_html)
    html = html.replace('<!-- SEARCH_VALUE -->', search)
    html = html.replace('<!-- TOTAL -->', str(len(filtered)))
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_stone_detail(request):
    """Карточка камня."""
    stone_id = request.match_info['stone_id']
    stones = load_all_stones()
    stone = stones.get(stone_id)

    if not stone:
        raise web.HTTPNotFound()

    # Related stones (same chakra or properties)
    chakra = stone.get('CHAKRA', '')
    related = []
    for sid, d in stones.items():
        if sid != stone_id and chakra and chakra in d.get('CHAKRA', ''):
            related.append((sid, d))
        if len(related) >= 3:
            break

    related_html = _render_stones_grid(related[:3]) if related else ''

    with open(TEMPLATES_DIR / 'stone_detail.html', encoding='utf-8') as f:
        html = f.read()

    html = html.replace('<!-- STONE_EMOJI -->', stone.get('EMOJI', '💎'))
    html = html.replace('<!-- STONE_TITLE -->', stone.get('TITLE', stone_id))
    html = html.replace('<!-- STONE_SHORT -->', stone.get('SHORT_DESC', ''))
    html = html.replace('<!-- STONE_FULL -->', stone.get('FULL_DESC', '').replace('\n', '<br>'))
    html = html.replace('<!-- STONE_PROPERTIES -->', stone.get('PROPERTIES', ''))
    html = html.replace('<!-- STONE_CHAKRA -->', stone.get('CHAKRA', ''))
    html = html.replace('<!-- STONE_COLOR -->', stone.get('COLOR', ''))
    html = html.replace('<!-- STONE_ZODIAC -->', stone.get('ZODIAC', ''))
    html = html.replace('<!-- STONE_PRICE -->', stone.get('PRICE_PER_BEAD', ''))
    html = html.replace('<!-- STONE_NOTES -->', stone.get('NOTES', ''))
    html = html.replace('<!-- STONE_ID -->', stone_id)
    html = html.replace('<!-- RELATED_STONES -->', related_html)

    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_quiz(request):
    """Страница квиза."""
    with open(TEMPLATES_DIR / 'quiz.html', encoding='utf-8') as f:
        html = f.read()
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_order(request):
    """Страница заказа."""
    stone_id = request.rel_url.query.get('stone', '')
    stones = load_all_stones()
    stone_name = stones.get(stone_id, {}).get('TITLE', '') if stone_id else ''

    with open(TEMPLATES_DIR / 'order.html', encoding='utf-8') as f:
        html = f.read()

    html = html.replace('<!-- PREFILL_STONE -->', stone_name)
    html = html.replace('<!-- STONE_ID -->', stone_id)
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_api_order(request):
    """API: создание заказа с сайта."""
    try:
        data = await request.json()
        name = data.get('name', '').strip()
        phone = data.get('phone', '').strip()
        request_text = data.get('request', '').strip()
        stone = data.get('stone', '').strip()
        size = data.get('size', '').strip()
        budget = data.get('budget', '').strip()

        if not name or not phone:
            return web.json_response({'ok': False, 'error': 'Укажи имя и телефон'}, status=400)

        # Сохраняем в БД как кастомный заказ
        conn = sqlite3.connect(str(get_db_path()))
        c = conn.cursor()
        c.execute("""
            INSERT INTO custom_orders
                (user_id, purpose, stones, size, notes, status, created_at)
            VALUES (0, ?, ?, ?, ?, 'pending', ?)
        """, (
            f'Заказ с сайта | {name} | {phone}',
            stone,
            size,
            f'{request_text}\nБюджет: {budget}',
            datetime.now()
        ))
        order_id = c.lastrowid
        conn.commit()
        conn.close()

        # Уведомление мастеру через Telegram
        await _notify_telegram(
            f"🌐 *ЗАКАЗ С САЙТА #{order_id}*\n\n"
            f"👤 {name}\n"
            f"📱 {phone}\n"
            f"💎 Камень: {stone or '—'}\n"
            f"📏 Размер: {size or '—'}\n"
            f"💰 Бюджет: {budget or '—'}\n\n"
            f"📝 {request_text or '—'}"
        )

        return web.json_response({'ok': True, 'order_id': order_id})

    except Exception as e:
        logger.error(f"Order API error: {e}")
        return web.json_response({'ok': False, 'error': 'Ошибка сервера'}, status=500)


async def handle_api_quiz_result(request):
    """API: результат квиза — возвращает рекомендованный камень."""
    try:
        data = await request.json()
        answers = data.get('answers', {})

        # Веса по ответам
        STONE_WEIGHTS = {
            'goal_love': {'rose_quartz': 5, 'moonstone': 4, 'rhodonite': 3},
            'goal_money': {'citrine': 5, 'tiger_eye': 4, 'pyrite': 3},
            'goal_protect': {'black_tourmaline': 5, 'obsidian': 4, 'hematite': 3},
            'goal_calm': {'amethyst': 5, 'lepidolite': 4, 'blue_aventurine': 3},
            'goal_health': {'clear_quartz': 5, 'jade': 4, 'green_tourmaline': 3},
            'goal_spirit': {'labradorite': 5, 'amethyst': 4, 'clear_quartz': 3},
            'goal_confidence': {'tiger_eye': 5, 'carnelian': 4, 'garnet': 3},
            'mood_stress': {'lepidolite': 5, 'amethyst': 4, 'blue_aventurine': 3},
            'mood_energy': {'carnelian': 5, 'garnet': 4, 'citrine': 3},
            'mood_sad': {'rose_quartz': 5, 'rhodonite': 4, 'moonstone': 3},
            'mood_anxious': {'lepidolite': 5, 'amethyst': 4, 'sodalite': 3},
            'color_purple': {'amethyst': 5, 'lepidolite': 4, 'sodalite': 3},
            'color_pink': {'rose_quartz': 5, 'rhodonite': 4, 'kunzite': 3},
            'color_black': {'black_tourmaline': 5, 'obsidian': 4, 'hematite': 3},
            'color_yellow': {'citrine': 5, 'tiger_eye': 4, 'pyrite': 3},
            'color_green': {'jade': 5, 'green_aventurine': 4, 'malachite': 3},
            'color_blue': {'labradorite': 5, 'sodalite': 4, 'lapis_lazuli': 3},
            'element_fire': {'carnelian': 5, 'garnet': 4, 'citrine': 3},
            'element_water': {'moonstone': 5, 'aquamarine': 4, 'labradorite': 3},
            'element_earth': {'hematite': 5, 'jade': 4, 'obsidian': 3},
            'element_air': {'clear_quartz': 5, 'sodalite': 4, 'amethyst': 3},
        }

        totals = {}
        for answer_key in answers.values():
            weights = STONE_WEIGHTS.get(answer_key, {})
            for stone_id, weight in weights.items():
                totals[stone_id] = totals.get(stone_id, 0) + weight

        if not totals:
            winner = 'amethyst'
        else:
            winner = max(totals, key=lambda k: totals[k])

        stones = load_all_stones()
        stone = stones.get(winner, {})

        return web.json_response({
            'ok': True,
            'stone_id': winner,
            'title': stone.get('TITLE', winner),
            'emoji': stone.get('EMOJI', '💎'),
            'short_desc': stone.get('SHORT_DESC', ''),
            'url': f'/stones/{winner}'
        })

    except Exception as e:
        logger.error(f"Quiz API error: {e}")
        return web.json_response({'ok': False, 'error': str(e)}, status=500)


async def _notify_telegram(text: str):
    """Уведомить мастера в Telegram."""
    try:
        from src.config import Config
        bot_token = Config.BOT_TOKEN
        admin_id = Config.ADMIN_ID
        if not bot_token or not admin_id:
            return
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                'chat_id': admin_id,
                'text': text,
                'parse_mode': 'Markdown'
            })
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


# ── Helpers для рендера HTML ──────────────────────────────

def _render_products_grid(products, limit=None):
    items = products[:limit] if limit else products
    if not items:
        return '<p class="empty">Товары появятся скоро</p>'
    html = '<div class="products-grid">'
    for p in items:
        price_html = f'<span class="price">{int(p["price"])} ₽</span>' if p.get('price') else ''
        stars_html = f'<span class="stars">{p["stars_price"]} ⭐</span>' if p.get('stars_price') else ''
        if p.get('image_file_id'):
            img = f'<div class="product-img"><img src="/photo/{p["image_file_id"]}" alt="{p["name"]}" loading="lazy"></div>'
        else:
            img = '<div class="product-img no-img">💎</div>'
        name = p["name"]
        desc = (p.get("description") or "")[:100]
        pid = p["id"]
        html += (
            f'<div class="product-card">' +
            img +
            f'<div class="product-info">' +
            f'<h3>{name}</h3>' +
            f'<p class="product-desc">{desc}...</p>' +
            f'<div class="product-price">{price_html}{stars_html}</div>' +
            f'<a href="/order?product={pid}" class="btn-order">Заказать</a>' +
            '</div></div>'
        )
    html += '</div>'
    return html


def _render_stones_grid(stones_list):
    if not stones_list:
        return ''
    html = '<div class="stones-grid">'
    for stone_id, d in stones_list:
        emoji = d.get('EMOJI', '💎')
        title = d.get('TITLE', stone_id)
        short = d.get('SHORT_DESC', '')
        props_raw = d.get('PROPERTIES', '')
        props = ', '.join(p.strip() for p in props_raw.split(',')[:3]) if props_raw else ''
        html += f'''<a class="stone-card" href="/stones/{stone_id}">
            <span class="stone-emoji">{emoji}</span>
            <h3>{title}</h3>
            <p>{short}</p>
            {f'<div class="stone-props">{props}</div>' if props else ''}
        </a>'''
    html += '</div>'
    return html


def _render_stones_list(stones_list):
    if not stones_list:
        return '<p class="empty">Ничего не найдено</p>'
    html = '<div class="stones-grid stones-grid--full">'
    for stone_id, d in stones_list:
        emoji = d.get('EMOJI', '💎')
        title = d.get('TITLE', stone_id)
        short = d.get('SHORT_DESC', '')
        chakra = d.get('CHAKRA', '')
        html += f'''<a class="stone-card" href="/stones/{stone_id}">
            <span class="stone-emoji">{emoji}</span>
            <h3>{title}</h3>
            <p>{short}</p>
            {f'<div class="stone-chakra">⚡ {chakra}</div>' if chakra else ''}
        </a>'''
    html += '</div>'
    return html


def _render_collections_tabs(collections, active_id):
    html = '<div class="collections-tabs">'
    html += f'<a class="tab {"active" if not active_id else ""}" href="/catalog">Все</a>'
    for col in collections:
        active = 'active' if active_id == col['id'] else ''
        html += f'<a class="tab {active}" href="/catalog?col={col["id"]}">{col["emoji"]} {col["name"]}</a>'
    html += '</div>'
    return html




async def handle_photo(request):
    """Прокси для фото из Telegram."""
    file_id = request.match_info['file_id']
    try:
        from src.config import Config
        # Get file path from Telegram
        token = Config.BOT_TOKEN
        async with aiohttp.ClientSession() as session:
            # getFile
            r = await session.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id}
            )
            data = await r.json()
            if not data.get("ok"):
                raise web.HTTPNotFound()
            file_path = data["result"]["file_path"]
            # Download file
            img_r = await session.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}"
            )
            img_bytes = await img_r.read()
            content_type = "image/jpeg"
            if file_path.endswith(".png"):
                content_type = "image/png"
            return web.Response(body=img_bytes, content_type=content_type,
                                headers={"Cache-Control": "public, max-age=86400"})
    except web.HTTPNotFound:
        raise
    except Exception as e:
        logger.error(f"Photo proxy error: {e}")
        raise web.HTTPNotFound()


async def handle_services(request):
    """Страница услуг."""
    services = db_query(
        "SELECT * FROM services WHERE active = 1 ORDER BY sort_order, id"
    )
    with open(TEMPLATES_DIR / 'services.html', encoding='utf-8') as f:
        html = f.read()
    services_html = _render_services(services)
    html = html.replace('<!-- SERVICES_LIST -->', services_html)
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_diagnostic(request):
    """Страница диагностики."""
    with open(TEMPLATES_DIR / 'diagnostic.html', encoding='utf-8') as f:
        html = f.read()
    return web.Response(text=html, content_type='text/html', charset='utf-8')


async def handle_faq(request):
    """FAQ страница."""
    faqs = db_query("SELECT * FROM faq WHERE active = 1 ORDER BY sort_order")
    with open(TEMPLATES_DIR / 'faq.html', encoding='utf-8') as f:
        html = f.read()
    faq_html = _render_faq(faqs)
    html = html.replace('<!-- FAQ_LIST -->', faq_html)
    return web.Response(text=html, content_type='text/html', charset='utf-8')


def _render_services(services):
    if not services:
        return '<p class="empty">Услуги появятся скоро</p>'
    html = '<div class="services-grid">'
    for s in services:
        price = f'{int(s["price"])} ₽' if s.get('price') else 'По запросу'
        duration = f'{s["duration"]} мин.' if s.get('duration') else ''
        html += f"""<div class="service-card">
            <div class="service-icon">✨</div>
            <h3>{s['name']}</h3>
            <p>{s.get('description', '')}</p>
            <div class="service-meta">
                <span class="service-price">{price}</span>
                {f'<span class="service-duration">{duration}</span>' if duration else ''}
            </div>
            <a href="/order?service={s['id']}" class="btn-order">Записаться</a>
        </div>"""
    html += '</div>'
    return html


def _render_faq(faqs):
    if not faqs:
        return '<p class="empty">Вопросы появятся скоро</p>'
    html = '<div class="faq-list">'
    for faq in faqs:
        html += f"""<details class="faq-item">
            <summary class="faq-question">{faq['question']}</summary>
            <div class="faq-answer">{faq['answer']}</div>
        </details>"""
    html += '</div>'
    return html

def create_web_app():
    """Создать aiohttp приложение."""
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/catalog', handle_catalog)
    app.router.add_get('/stones', handle_stones)
    app.router.add_get('/stones/{stone_id}', handle_stone_detail)
    app.router.add_get('/quiz', handle_quiz)
    app.router.add_get('/order', handle_order)
    app.router.add_post('/api/order', handle_api_order)
    app.router.add_post('/api/quiz', handle_api_quiz_result)
    app.router.add_get('/static/{filename:.+}', handle_static)
    app.router.add_get('/services', handle_services)
    app.router.add_get('/diagnostic', handle_diagnostic)
    app.router.add_get('/faq', handle_faq)
    app.router.add_get('/photo/{file_id}', handle_photo)
    return app
