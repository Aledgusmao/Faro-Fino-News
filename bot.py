# Faro Fino News v1.7 - Reset Controlado pelo Usuário
# Adiciona um comando /limpar_tudo para que o usuário possa forçar a exclusão do arquivo de configuração.

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
from telegram.error import TelegramError, Conflict
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from urllib.parse import quote

# --- CONFIGURAÇÕES ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CONFIG_PATH = "faro_fino_config_v2.json"
MONITORAMENTO_INTERVAL = 300
DIAS_FILTRO_NOTICIAS = 7
TIMEZONE_BR = pytz.timezone('America/Sao_Paulo')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
DEFAULT_CONFIG = {"owner_id": None, "keywords": [], "monitoring_on": False, "history": set()}

# --- FUNÇÕES DE DADOS ---
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
                config['history'] = set(config.get('history', []))
                return config
        except (json.JSONDecodeError, IOError):
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(config):
    to_save = config.copy()
    to_save['history'] = list(to_save.get('history', set()))
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(to_save, f, indent=4)

# --- MOTOR DE BUSCA ---
async def fetch_news(keywords: list) -> list:
    news_items = []
    if not keywords: return news_items

    query_parts = [f'"{k.strip()}"' for k in keywords if k.strip()]
    query = " OR ".join(query_parts)
    
    seven_days_ago = (datetime.now() - timedelta(days=DIAS_FILTRO_NOTICIAS)).strftime('%Y-%m-%d')
    full_query = f'{query} after:{seven_days_ago}'
    
    encoded_query = quote(full_query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    logger.info(f"Buscando notícias com URL: {url}")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml-xml')
        for item in soup.find_all('item'):
            try:
                pub_date_str = item.find('pubDate').text
                pub_date = parsedate_to_datetime(pub_date_str).astimezone(TIMEZONE_BR)
                news_items.append({'title': item.title.text, 'link': item.link.text, 'source': item.source.text, 'date': pub_date})
            except (AttributeError, TypeError): continue
    except Exception as e:
        logger.error(f"Erro na busca de notícias: {e}", exc_info=True)
    return news_items

async def process_news(context: ContextTypes.DEFAULT_TYPE, is_manual=False, chat_id_manual=None):
    config = load_config()
    owner_id, keywords = config.get("owner_id"), config.get("keywords")
    target_chat_id = chat_id_manual if is_manual else owner_id

    if not owner_id or not keywords:
        if is_manual and target_chat_id: await context.bot.send_message(chat_id=target_chat_id, text="Nenhuma palavra-chave configurada.")
        return

    if not config.get("monitoring_on") and not is_manual: return
    
    found_news = await fetch_news(keywords)
    logger.info(f"Busca no Google retornou {len(found_news)} itens brutos.")

    new_articles, history = [], config.get('history', set())
    
    for article in found_news:
        if article['link'] in history: continue
        text_to_check = f"{article['title']} {article['source']}".lower()
        found_kws = [k for k in keywords if k.lower() in text_to_check]
        
        if found_kws:
            article['found_keywords'] = list(set(found_kws))
            new_articles.append(article)
            history.add(article['link'])

    logger.info(f"Após filtros, {len(new_articles)} são novas.")
    if new_articles:
        await send_notifications(owner_id, new_articles, context)
    
    config['history'] = history
    save_config(config)
    
    if is_manual and target_chat_id:
        await context.bot.send_message(chat_id=target_chat_id, text=f"Verificação manual concluída. {len(new_articles)} novas notícias encontradas.")
    elif not is_manual:
        logger.info(f"[Auto] Verificação concluída. {len(new_articles)} novas notícias.")

async def send_notifications(chat_id, articles, context: ContextTypes.DEFAULT_TYPE):
    for article in sorted(articles, key=lambda x: x['date'], reverse=True):
        date_str = article['date'].strftime('%d/%m/%Y %H:%M')
        message = (f"✅ *{article['title']}*\n\n"
                   f"🚨 *Encontrado por:* `{', '.join(article['found_keywords'])}`\n"
                   f"📅 *Publicado em:* {date_str}\n"
                   f"🌐 *Fonte:* {article['source']}\n"
                   f"🔗 [Clique para ler]({article['link']})\n"
                   f"---")
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            await asyncio.sleep(1.5)
        except TelegramError as e:
            logger.error(f"Falha ao enviar notificação: {e}")

async def monitor_loop(app: Application):
    context = ContextTypes.DEFAULT_TYPE(application=app)
    await asyncio.sleep(15)
    while True:
        config = load_config()
        if config.get("monitoring_on") and config.get("owner_id"):
            logger.info("Iniciando verificação automática.")
            await process_news(context)
        await asyncio.sleep(MONITORAMENTO_INTERVAL)

def is_owner(update: Update, config: dict):
    return update.effective_user.id == config.get("owner_id")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.get('owner_id'):
        config['owner_id'] = update.effective_user.id
        save_config(config)
        await update.message.reply_text("Bem-vindo! Seu ID foi registrado. Use /menu para começar. Se encontrar problemas, use /limpar_tudo para um reset total.")
    else:
        await update.message.reply_text("Bem-vindo de volta! Use /menu para ver os comandos.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    text = update.message.text.strip()
    if not text.startswith(('@', '#')): return
    
    items_raw = text[1:].upper().replace(' OU ', ',').replace(' OR ', ',').split(',')
    items = sorted([k.strip() for k in items_raw if k.strip()])
    
    if not items:
        await update.message.reply_text("ℹ️ Formato inválido.")
        return

    keywords_set = set(config.get('keywords', []))
    changed = False
    
    if text.startswith('@'):
        added = [k for k in items if k not in keywords_set]
        if added: keywords_set.update(added); changed = True; msg = f"✅ Adicionados: {', '.join(added)}"
        else: msg = "ℹ️ Nenhuma palavra-chave nova para adicionar."
    else:
        original_keywords_to_remove = {k for k in keywords_set if k.lower() in [i.lower() for i in items]}
        if original_keywords_to_remove:
            keywords_set.difference_update(original_keywords_to_remove); changed = True; msg = f"🗑️ Removidos: {', '.join(sorted(list(original_keywords_to_remove)))}"
        else: msg = "ℹ️ Nenhuma das palavras-chave informadas foi encontrada para remoção."

    if changed:
        config['keywords'] = sorted(list(keywords_set))
        save_config(config)
    
    await update.message.reply_text(msg)
    
async def limpar_tudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config):
        await update.message.reply_text("Você não tem permissão para fazer isso.")
        return

    msg_inicial = await update.message.reply_text("⚠️ **ATENÇÃO!** Este comando apagará todo o seu histórico e palavras-chave. \n\nConfirmando em 3...")
    await asyncio.sleep(1)
    await msg_inicial.edit_text("Confirmando em 2...")
    await asyncio.sleep(1)
    await msg_inicial.edit_text("Confirmando em 1...")
    await asyncio.sleep(1)
    
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            await msg_inicial.edit_text("✅ **Configuração e histórico apagados com sucesso!**\n\nO bot foi resetado. Por favor, use o comando /start para se registrar como dono novamente.")
            logger.info(f"O arquivo de configuração {CONFIG_PATH} foi removido pelo dono.")
        else:
            await msg_inicial.edit_text("ℹ️ Nenhuma configuração encontrada para apagar. O bot já está no estado inicial. Use /start para começar.")
    except Exception as e:
        await msg_inicial.edit_text(f"❌ Erro ao tentar apagar a configuração: {e}")
        logger.error(f"Falha ao apagar o arquivo de configuração: {e}")

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query if update.callback_query else update
    await query.message.reply_text("Iniciando verificação manual profunda (últimos 7 dias)...")
    await process_news(context, is_manual=True, chat_id_manual=query.message.chat_id)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query if update.callback_query else update
    await query.message.reply_text("Gerando status...")
    
    status_text = (f"📊 *Status e Diagnóstico*\n\n"
                   f"∙ Monitoramento: {'🟢 Ativo' if config.get('monitoring_on') else '🔴 Inativo'}\n"
                   f"∙ Palavras-chave: {len(config.get('keywords', []))}\n"
                   f"∙ Histórico: {len(config.get('history', set()))} links")
    await query.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

async def view_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query if update.callback_query else update
    keywords = config.get('keywords', [])
    if keywords: msg = f"📝 *Palavras-Chave ({len(keywords)}):*\n`{', '.join(keywords)}`"
    else: msg = "Nenhuma palavra-chave configurada."
    await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Verificar Agora", callback_data='check_now')],
                [InlineKeyboardButton("Ligar/Desligar Monitoramento", callback_data='toggle_monitoring')],
                [InlineKeyboardButton("Ver Status e Diagnóstico", callback_data='status')],
                [InlineKeyboardButton("Listar Palavras-Chave", callback_data='view_keywords')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('⚙️ **Menu Principal**', reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'check_now': await check_now(update, context)
    elif query.data == 'status': await status(update, context)
    elif query.data == 'view_keywords': await view_keywords(update, context)
    elif query.data == 'toggle_monitoring':
        config = load_config()
        config['monitoring_on'] = not config.get('monitoring_on', False)
        save_config(config)
        status_text = '🟢 ATIVADO' if config['monitoring_on'] else '🔴 DESATIVADO'
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"Monitoramento automático foi {status_text}.")

async def post_init_task(app: Application):
    asyncio.create_task(monitor_loop(app))

def main():
    if not BOT_TOKEN:
        logger.error("ERRO: BOT_TOKEN não configurado!")
        return
        
    application = Application.builder().token(BOT_TOKEN).build()
    application.post_init = post_init_task
    
    application.add_handler(CommandHandler('limpar_tudo', limpar_tudo))
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('menu', menu_command))
    application.add_handler(CommandHandler('status', status))
    application.add_handler(CommandHandler('verificar', check_now))
    application.add_handler(CommandHandler('verpalavras', view_keywords))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("🚀 Faro Fino News v1.7 iniciando!")
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.critical(f"Erro fatal ao executar o bot: {e}", exc_info=True)

if __name__ == "__main__":
    main()