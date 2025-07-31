# Faro Fino News v3.0.1 - Correção do Fluxo de Desbloqueio Assistido
# Corrige a lógica dos botões na mensagem de ajuda do desbloqueio.

import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from urllib.parse import quote

# --- CONFIGURAÇÕES e CÓDIGO BASE (INTOCÁVEL) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CONFIG_PATH = "faro_fino_config_v2.json"
LOCK_FILE_PATH = "bot.lock"
MONITORAMENTO_INTERVAL = 300
DIAS_FILTRO_NOTICIAS = 3
CHUNK_SIZE_KEYWORDS = 5
TIMEZONE_BR = pytz.timezone('America/Sao_Paulo')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
DEFAULT_CONFIG = {"owner_id": None, "notification_chat_id": None, "keywords": [], "monitoring_on": False, "history": set()}
COLOR_EMOJIS = ["🔵", "🟢", "🔴", "🟣", "🟠", "🟡"]
color_index = 0

def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f: config = json.load(f)
            config['history'] = set(config.get('history', []))
            return config
        except (json.JSONDecodeError, IOError): return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(config):
    to_save = config.copy()
    to_save['history'] = list(to_save.get('history', set()))
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f: json.dump(to_save, f, indent=4)

async def resolve_google_redirect(google_link: str, client: httpx.AsyncClient) -> str:
    try:
        response = await client.head(google_link, follow_redirects=True, timeout=10)
        return str(response.url)
    except httpx.RequestError as e:
        logger.warning(f"Não foi possível resolver o redirecionamento para {google_link}: {e}")
        return google_link

async def fetch_news_chunk(keywords_chunk: list, client: httpx.AsyncClient) -> list:
    news_items = []
    if not keywords_chunk: return news_items
    query = " OR ".join([f'"{k.strip()}"' for k in keywords_chunk])
    encoded_query = quote(query)
    cache_buster = int(datetime.now().timestamp())
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=pt-BR&gl=BR&ceid=BR:pt-419&tbs=qdr:d{DIAS_FILTRO_NOTICIAS}&cb={cache_buster}"
    try:
        response = await client.get(url, timeout=30.0)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml-xml')
        for item in soup.find_all('item'):
            try:
                google_link = item.link.text
                final_link = await resolve_google_redirect(google_link, client)
                pub_date = parsedate_to_datetime(item.find('pubDate').text).astimezone(TIMEZONE_BR)
                news_items.append({'title': item.title.text, 'link': final_link, 'source': item.source.text, 'date': pub_date})
            except (AttributeError, TypeError): continue
    except Exception as e:
        logger.error(f"Erro na busca do pedaço {keywords_chunk}: {e}")
    return news_items

async def process_news(context: ContextTypes.DEFAULT_TYPE, is_manual=False, chat_id_manual=None):
    config = load_config()
    notification_id = config.get("notification_chat_id")
    owner_id, keywords = config.get("owner_id"), config.get("keywords")
    target_chat_id = chat_id_manual if is_manual else notification_id
    if not owner_id or not keywords:
        if is_manual and target_chat_id: await context.bot.send_message(chat_id=target_chat_id, text="Nenhuma palavra-chave configurada.")
        return
    if not config.get("monitoring_on") and not is_manual: return
    
    keyword_chunks = [keywords[i:i + CHUNK_SIZE_KEYWORDS] for i in range(0, len(keywords), CHUNK_SIZE_KEYWORDS)]
    all_found_articles = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, chunk in enumerate(keyword_chunks):
            chunk_results = await fetch_news_chunk(chunk, client)
            for article in chunk_results: all_found_articles[article['link']] = article
            await asyncio.sleep(1)
    
    found_news = list(all_found_articles.values())
    new_articles, history = [], config.get('history', set())
    limit = datetime.now(TIMEZONE_BR) - timedelta(days=DIAS_FILTRO_NOTICIAS + 1)
    for article in found_news:
        if article['link'] in history or (article['date'] and article['date'] < limit): continue
        if found_kws := [k for k in keywords if k.lower() in f"{article['title']} {article['source']}".lower()]:
            article['found_keywords'] = list(set(found_kws))
            new_articles.append(article)
            history.add(article['link'])
            
    if new_articles and notification_id:
        await send_notifications(notification_id, new_articles, context)
    
    config['history'] = history
    save_config(config)
    
    if is_manual and target_chat_id:
        await context.bot.send_message(chat_id=target_chat_id, text=f"Verificação concluída. Encontradas {len(new_articles)} novas notícias.")

async def send_notifications(chat_id, articles, context: ContextTypes.DEFAULT_TYPE):
    global color_index
    for article in sorted(articles, key=lambda x: x['date'], reverse=True):
        date_str = article['date'].strftime('%d/%m/%Y %H:%M')
        color_emoji = COLOR_EMOJIS[color_index]
        color_index = (color_index + 1) % len(COLOR_EMOJIS)
        
        message = (f"{color_emoji} *{article['title']}*\n\n"
                   f"🚨 *Encontrado por:* `{', '.join(article['found_keywords'])}`\n"
                   f"📅 *Publicado em:* {date_str}\n"
                   f"🌐 *Fonte:* {article['source']}")
        
        final_link = article['link']
        keyboard = [[
            InlineKeyboardButton("🌐 Site Original", url=final_link),
            InlineKeyboardButton("🔓 Desbloquear Notícia", callback_data="unlock_article")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, 
                reply_markup=reply_markup, disable_web_page_preview=False
            )
            await asyncio.sleep(2)
        except TelegramError as e:
            logger.error(f"Falha ao enviar notificação para {final_link}: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    
    if query.data == "unlock_article":
        await query.answer()
        try:
            original_link = query.message.reply_markup.inline_keyboard[0][0].url
            help_message = (
                "**Para ler a notícia bloqueada:**\n\n"
                "1. Primeiro, clique no botão abaixo para abrir o serviço de desbloqueio em uma nova aba.\n\n"
                "2. Depois, volte aqui e clique no link que enviarei a seguir para copiá-lo."
            )
            unlock_keyboard = [[InlineKeyboardButton("Abrir Serviço de Desbloqueio", url="https://www.removepaywall.com/")]]
            
            await context.bot.send_message(
                chat_id=query.message.chat_id, text=help_message,
                parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(unlock_keyboard),
                reply_to_message_id=query.message.message_id
            )
            await context.bot.send_message(
                chat_id=query.message.chat_id, text=f"`{original_link}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except (AttributeError, IndexError):
             await query.answer("❌ Erro: não foi possível encontrar o link original.", show_alert=True)
        return

    config = load_config()
    if not is_owner(update, config):
        await query.answer("Você não tem permissão para usar este botão.", show_alert=True)
        return
    
    await query.answer()
    if query.data == 'check_now': await check_now(update, context)
    elif query.data == 'status': await status(update, context)
    elif query.data == 'view_keywords': await view_keywords(update, context)
    elif query.data == 'toggle_monitoring':
        config['monitoring_on'] = not config.get('monitoring_on', False)
        save_config(config)
        status_text = '🟢 ATIVADO' if config['monitoring_on'] else '🔴 DESATIVADO'
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"Monitoramento: {status_text}.")
        
async def monitor_loop(app: Application):
    context = ContextTypes.DEFAULT_TYPE(application=app)
    await asyncio.sleep(15)
    while True:
        config = load_config()
        if config.get("monitoring_on") and config.get("owner_id"):
            await process_news(context)
        await asyncio.sleep(MONITORAMENTO_INTERVAL)

def is_owner(update: Update, config: dict) -> bool:
    return update.effective_user.id == config.get("owner_id")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    user_name = update.effective_user.first_name
    if not config.get('owner_id'):
        config['owner_id'] = update.effective_user.id
        config['notification_chat_id'] = update.effective_chat.id
        save_config(config)
        welcome_text = (f"Olá, {user_name}! 👋\n\n"
                        "Eu sou o *Faro Fino News*, seu farejador de notícias particular.\n\n"
                        "Seu ID foi registrado. Use /menu para começar.\n"
                        "Para receber notícias em um grupo, me adicione lá e use /definir_grupo.")
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"Bem-vindo de volta, {user_name}!")

async def definir_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    chat_id = update.effective_chat.id
    config['notification_chat_id'] = chat_id
    save_config(config)
    await update.message.reply_text("✅ A partir de agora, as notícias serão enviadas aqui.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    text = update.message.text.strip()
    if not text.startswith(('@', '#')): return
    items_raw = text[1:].upper().replace(' OU ', ',').replace(' OR ', ',').split(',')
    items = sorted([k.strip() for k in items_raw if k.strip()])
    if not items: await update.message.reply_text("ℹ️ Formato inválido."); return
    keywords_set, changed = set(config.get('keywords', [])), False
    if text.startswith('@'):
        added = [k for k in items if k not in keywords_set]
        if added: keywords_set.update(added); changed = True; msg = f"✅ Adicionados: {', '.join(added)}"
        else: msg = "ℹ️ Nenhuma palavra-chave nova para adicionar."
    else:
        to_remove = {k for k in keywords_set if k.lower() in [i.lower() for i in items]}
        if to_remove: keywords_set.difference_update(to_remove); changed = True; msg = f"🗑️ Removidos: {', '.join(sorted(list(to_remove)))}"
        else: msg = "ℹ️ Nenhuma das palavras-chave informadas foi encontrada."
    if changed: config['keywords'] = sorted(list(keywords_set)); save_config(config)
    await update.message.reply_text(msg)
    
async def limpar_tudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    msg = await update.message.reply_text("⚠️ **ATENÇÃO!** Confirmando reset total em 3...")
    for i in range(2, 0, -1): await asyncio.sleep(1); await msg.edit_text(f"Confirmando em {i}...")
    await asyncio.sleep(1)
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            await msg.edit_text("✅ **Configuração e histórico apagados!**\n\nUse /start para recomeçar.")
        else: await msg.edit_text("ℹ️ Nenhuma configuração encontrada para apagar.")
    except Exception as e: await msg.edit_text(f"❌ Erro ao apagar: {e}")

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    await query.message.reply_text("🐶 Farejando as últimas notícias para você... Por favor, aguarde.")
    await process_news(context, is_manual=True, chat_id_manual=query.message.chat_id)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    await query.message.reply_text("Gerando status...")
    dest_chat_id = config.get('notification_chat_id', 'Não definido')
    status_text = (f"📊 *Status v3.0.1*\n\n"
                   f"∙ Monitoramento: {'🟢 Ativo' if config.get('monitoring_on') else '🔴 Inativo'}\n"
                   f"∙ Palavras-chave: {len(config.get('keywords', []))}\n"
                   f"∙ Histórico: {len(config.get('history', set()))} links\n"
                   f"∙ Destino Notificações: `{dest_chat_id}`\n"
                   f"∙ Buscas por verificação: {-(len(config.get('keywords', [])) // -CHUNK_SIZE_KEYWORDS)}")
    if hasattr(query, 'message') and query.message: await query.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
    else: await context.bot.send_message(chat_id=update.effective_chat.id, text=status_text, parse_mode=ParseMode.MARKDOWN)

async def view_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    keywords = config.get('keywords', [])
    msg = f"📝 *Palavras-Chave ({len(keywords)}):*\n`{', '.join(keywords)}`" if keywords else "Nenhuma palavra-chave."
    await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    kb = [[InlineKeyboardButton("Verificar Agora", callback_data='check_now')],
          [InlineKeyboardButton("Ligar/Desligar Monitoramento", callback_data='toggle_monitoring')],
          [InlineKeyboardButton("Listar Palavras-Chave", callback_data='view_keywords')]]
    await update.message.reply_text('⚙️ **Menu Principal**', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def post_init_task(app: Application):
    asyncio.create_task(monitor_loop(app))

def main():
    if os.path.exists(LOCK_FILE_PATH):
        logger.error(f"Arquivo de trava '{LOCK_FILE_PATH}' encontrado. Encerrando."); return
    try:
        with open(LOCK_FILE_PATH, 'w') as f: f.write(str(os.getpid()))
        if not BOT_TOKEN: logger.error("ERRO: BOT_TOKEN não configurado!"); return
        app = Application.builder().token(BOT_TOKEN).build()
        app.post_init = post_init_task
        handlers = [CommandHandler('start', start), CommandHandler('menu', menu_command),
                    CommandHandler('status', status), CommandHandler('verificar', check_now), 
                    CommandHandler('verpalavras', view_keywords), CommandHandler('definir_grupo', definir_grupo),
                    CommandHandler('limpar_tudo', limpar_tudo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler), CallbackQueryHandler(button_handler)]
        app.add_handlers(handlers)
        logger.info("🚀 Faro Fino News v3.0.1 iniciando!")
        app.run_polling(drop_pending_updates=True)
    finally:
        if os.path.exists(LOCK_FILE_PATH):
            os.remove(LOCK_FILE_PATH)
            logger.info(f"Arquivo de trava '{LOCK_FILE_PATH}' removido.")

if __name__ == "__main__": main()
