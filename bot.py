# Faro Fino News v2.6 - Integração Correta com API 12ft.io
# Usa callback e requisição POST para interagir corretamente com o serviço 12ft.io.

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

# --- CONFIGURAÇÕES e FUNÇÕES DE DADOS (sem alterações) ---
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

# --- MOTOR DE BUSCA (sem alterações) ---
async def fetch_news_chunk(keywords_chunk: list) -> list:
    news_items = []
    if not keywords_chunk: return news_items
    query = " OR ".join([f'"{k.strip()}"' for k in keywords_chunk])
    encoded_query = quote(query)
    cache_buster = int(datetime.now().timestamp())
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=pt-BR&gl=BR&ceid=BR:pt-419&tbs=qdr:d{DIAS_FILTRO_NOTICIAS}&cb={cache_buster}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml-xml')
        for item in soup.find_all('item'):
            try:
                pub_date = parsedate_to_datetime(item.find('pubDate').text).astimezone(TIMEZONE_BR)
                news_items.append({'title': item.title.text, 'link': item.link.text, 'source': item.source.text, 'date': pub_date})
            except (AttributeError, TypeError): continue
    except Exception as e:
        logger.error(f"Erro na busca do pedaço {keywords_chunk}: {e}")
    return news_items

# --- PROCESSAMENTO DE NOTÍCIAS (sem alterações) ---
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
    logger.info(f"Iniciando busca com {len(keywords)} palavras-chave em {len(keyword_chunks)} pedaços.")
    for i, chunk in enumerate(keyword_chunks):
        chunk_results = await fetch_news_chunk(chunk)
        for article in chunk_results: all_found_articles[article['link']] = article
        await asyncio.sleep(1)
    found_news = list(all_found_articles.values())
    logger.info(f"Busca em pedaços retornou {len(found_news)} artigos únicos.")
    new_articles, history = [], config.get('history', set())
    limit = datetime.now(TIMEZONE_BR) - timedelta(days=DIAS_FILTRO_NOTICIAS + 1)
    for article in found_news:
        if article['link'] in history or (article['date'] and article['date'] < limit): continue
        if found_kws := [k for k in keywords if k.lower() in f"{article['title']} {article['source']}".lower()]:
            article['found_keywords'] = list(set(found_kws))
            new_articles.append(article)
            history.add(article['link'])
    logger.info(f"Após filtros, {len(new_articles)} são novas.")
    if new_articles and notification_id:
        await send_notifications(notification_id, new_articles, context)
    config['history'] = history
    save_config(config)
    if is_manual and target_chat_id:
        await context.bot.send_message(chat_id=target_chat_id, text=f"Verificação concluída. Encontradas {len(new_articles)} novas notícias.")

async def send_notifications(chat_id, articles, context: ContextTypes.DEFAULT_TYPE):
    """Envia as notificações de notícias com botões de ação (Original e callback para 12ft.io)."""
    for article in sorted(articles, key=lambda x: x['date'], reverse=True):
        date_str = article['date'].strftime('%d/%m/%Y %H:%M')
        message = (f"✅ *{article['title']}*\n\n"
                   f"🚨 *Encontrado por:* `{', '.join(article['found_keywords'])}`\n"
                   f"📅 *Publicado em:* {date_str}\n"
                   f"🌐 *Fonte:* {article['source']}")
        original_link = article['link']
        # *** ALTERAÇÃO 1: O botão de desbloqueio agora usa callback_data ***
        keyboard = [
            [
                InlineKeyboardButton("🌐 Site Original", url=original_link),
                InlineKeyboardButton("🔓 Ler Sem Bloqueio", callback_data="unlock_article")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
            await asyncio.sleep(1.5)
        except TelegramError as e:
            logger.error(f"Falha ao enviar notificação para {original_link}: {e}")

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

# --- NOVO HELPER PARA A API 12FT.IO ---
async def get_12ft_link(original_link: str) -> str | None:
    """Faz a requisição POST para a API do 12ft.io para obter o link desbloqueado."""
    api_url = "https://12ft.io/api/proxy"
    headers = {"Referer": "https://12ft.io/"}
    payload = {"url": original_link}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(api_unlocked_link_response = response.json()
            if api_response.get("status") == "success" and api_response.get("url"):
                return api_response["url"]
            else:
                logger.error(f"API 12ft.io retornou erro: {api_response}")
                return None
    except Exception as e:
        logger.error(f"Erro ao contatar API 12ft.io: {e}")
        return None

# --- COMANDOS E HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
    config = load_config()
    if not config.get('owner_id'):
        config['owner_id'] = update.effective_user.id
        config['notification_chat_id'] = update.effective_chat.id
        save_config(config)
        await update.message.reply_text("Bem-vindo! As notificações serão enviadas aqui. Para mudar, use /definir_grupo.")
    else: await update.message.reply_text("Bem-vindo de volta!")

async def definir_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
    config = load_config()
    if not is_owner(update, config): return
    chat_id = update.effective_chat.id
    config['notification_chat_id'] = chat_id
    save_config(config)
    await update.message.reply_text("✅ A partir de agora, as notícias serão enviadas aqui.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
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
    # ... (código inalterado) ...
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
    # ... (código inalterado) ...
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    await query.message.reply_text("🐶 Farejando as últimas notícias para você... Por favor, aguarde.")
    await process_news(context, is_manual=True, chat_id_manual=query.message.chat_id)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    await query.message.reply_text("Gerando status...")
    dest_chat_id = config.get('notification_chat_id', 'Não definido')
    status_text = (f"📊 *Status v2.6*\n\n"
                   f"∙ Monitoramento: {'🟢 Ativo' if config.get('monitoring_on') else '🔴 Inativo'}\n"
                   f"∙ Palavras-chave: {len(config.get('keywords', []))}\n"
                   f"∙ Histórico: {len(config.get('history', set()))} links\n"
                   f"∙ Destino Notificações: `{dest_chat_id}`\n"
                   f"∙ Buscas por verificação: {-(len(config.get('keywords', [])) // -CHUNK_SIZE_KEYWORDS)}")
    if hasattr(query, 'message') and query.message: await query.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
    else: await context.bot.send_message(chat_id=update.effective_chat.id, text=status_text, parse_mode=ParseMode.MARKDOWN)

async def view_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    keywords = config.get('keywords', [])
    msg = f"📝 *Palavras-Chave ({len(keywords)}):*\n`{', '.join(keywords)}`" if keywords else "Nenhuma palavra-chave."
    await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (código inalterado) ...
    config = load_config()
    if not is_owner(update, config): return
    kb = [[InlineKeyboardButton("Verificar Agora", callback_data='check_now')],
          [InlineKeyboardButton("Ligar/Desligar Monitoramento", callback_data='toggle_monitoring')],
          [InlineKeyboardButton("Ver Status e Diagnóstico", callback_data='status')],
          [InlineKeyboardButton("Listar Palavras-Chave", callback_data='view_keywords')]]
    await update.message.reply_text('⚙️ **Menu Principal**', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com todos os botões, incluindo os do menu e o novo de desbloqueio."""
    query = update.callback_query
    await query.answer()

    # Separa os callbacks do menu dos callbacks de artigo
    if query.data.startswith("unlock_"):
        if query.data == "unlock_article":
            # Pega a URL do primeiro botão na mensagem (Site Original)
            try:
                original_link = query.message.reply_markup.inline_keyboard[0][0].url
                await query.edit_message_text(text=f"{query.message.text}\n\n*_🔓 Desbloqueando notícia..._*", parse_mode=ParseMode.MARKDOWN)
                
                unlocked_link = await get_12ft_link(original_link)
                
                if unlocked_link:
                    # Sucesso: Edita a mensagem e adiciona um botão para o link desbloqueado
                    kb = [[InlineKeyboardButton("✅ Notícia Desbloqueada - Clique Aqui", url=unlocked_link)]]
                    await query.edit_message_text(text=f"{query.message.text}\n\n*_Sucesso!_*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
                else:
                    # Falha: Informa o usuário
                    await query.edit_message_text(text=f"{query.message.text}\n\n*_❌ Falha ao tentar desbloquear o link._*", parse_mode=ParseMode.MARKDOWN)
            except (AttributeError, IndexError):
                 await query.edit_message_text(text=f"{query.message.text}\n\n*_❌ Erro: não foi possível encontrar o link original na mensagem._*", parse_mode=ParseMode.MARKDOWN)
            except BadRequest: # Evita crash se o usuário clicar rápido demais
                logger.warning("BadRequest: Message is not modified (ignorado).")
                pass

    else: # Lógica para os botões do menu
        config = load_config()
        if not is_owner(update, config): return
        
        if query.data == 'check_now': await check_now(update, context)
        elif query.data == 'status': await status(update, context)
        elif query.data == 'view_keywords': await view_keywords(update, context)
        elif query.data == 'toggle_monitoring':
            config['monitoring_on'] = not config.get('monitoring_on', False)
            save_config(config)
            status_text = '🟢 ATIVADO' if config['monitoring_on'] else '🔴 DESATIVADO'
            await context.bot.send_message(chat_id=query.message.chat_id, text=f"Monitoramento: {status_text}.")

async def post_init_task(app: Application):
    # ... (código inalterado) ...
    asyncio.create_task(monitor_loop(app))

def main():
    # ... (código inalterado) ...
    if os.path.exists(LOCK_FILE_PATH):
        logger.error(f"Arquivo de trava '{LOCK_FILE_PATH}' encontrado. Encerrando.")
        return
    try:
        with open(LOCK_FILE_PATH, 'w') as f: f.write(str(os.getpid()))
        if not BOT_TOKEN: logger.error("ERRO: BOT_TOKEN não configurado!"); return
        app = Application.builder().token(BOT_TOKEN).build()
        app.post_init = post_init_task
        handlers = [CommandHandler('limpar_tudo', limpar_tudo), CommandHandler('start', start), CommandHandler('menu', menu_command),
                    CommandHandler('status', status), CommandHandler('verificar', check_now), CommandHandler('verpalavras', view_keywords),
                    CommandHandler('definir_grupo', definir_grupo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler), CallbackQueryHandler(button_handler)]
        app.add_handlers(handlers)
        logger.info("🚀 Faro Fino News v2.6 iniciando!")
        app.run_polling(drop_pending_updates=True)
    finally:
        if os.path.exists(LOCK_FILE_PATH):
            os.remove(LOCK_FILE_PATH)
            logger.info(f"Arquivo de trava '{LOCK_FILE_PATH}' removido.")

if __name__ == "__main__": main()
