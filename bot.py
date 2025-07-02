# Faro Fino News v1.8 - Filtro de Data Corrigido e Status Aprimorado
# Restaura o filtro de data funcional (tbs=qdr:d7) e mantém o status aprimorado.

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
DIAS_FILTRO_NOTICIAS = 7 # Mantemos 7 dias para a busca ser ampla
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

# --- MOTOR DE BUSCA (COM FILTRO CORRIGIDO) ---
async def fetch_news(keywords: list) -> list:
    news_items = []
    if not keywords: return news_items
    
    query_parts = [f'"{k.strip()}"' for k in keywords if k.strip()]
    query = " OR ".join(query_parts)
    
    encoded_query = quote(query)
    
    # *** CORREÇÃO CRÍTICA DO FILTRO DE DATA ***
    # Voltamos a usar o parâmetro 'tbs=qdr:d7' que é o correto para filtrar os últimos 7 dias no RSS.
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=pt-BR&gl=BR&ceid=BR:pt-419&tbs=qdr:d{DIAS_FILTRO_NOTICIAS}"
    # *** FIM DA CORREÇÃO ***
    
    logger.info(f"Buscando notícias com URL: {url}")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml-xml')
        for item in soup.find_all('item'):
            try:
                pub_date = parsedate_to_datetime(item.find('pubDate').text).astimezone(TIMEZONE_BR)
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
    # O filtro de data na URL já é o principal, mas mantemos este como uma segunda camada de segurança.
    limit = datetime.now(TIMEZONE_BR) - timedelta(days=DIAS_FILTRO_NOTICIAS + 1)

    for article in found_news:
        if article['link'] in history or (article['date'] and article['date'] < limit): continue
        text_to_check = f"{article['title']} {article['source']}".lower()
        if found_kws := [k for k in keywords if k.lower() in text_to_check]:
            article['found_keywords'] = list(set(found_kws))
            new_articles.append(article)
            history.add(article['link'])
            
    logger.info(f"Após filtros, {len(new_articles)} são novas.")
    if new_articles: await send_notifications(owner_id, new_articles, context)
    
    config['history'] = history
    save_config(config)
    
    if is_manual and target_chat_id: await context.bot.send_message(chat_id=target_chat_id, text=f"Verificação concluída. {len(new_articles)} novas notícias encontradas.")
    elif not is_manual: logger.info(f"[Auto] Verificação concluída. {len(new_articles)} novas notícias.")

# --- O RESTANTE DO CÓDIGO (sem alterações) ---

async def send_notifications(chat_id, articles, context: ContextTypes.DEFAULT_TYPE):
    for article in sorted(articles, key=lambda x: x['date'], reverse=True):
        date_str = article['date'].strftime('%d/%m/%Y %H:%M')
        message = (f"✅ *{article['title']}*\n\n🚨 *Encontrado por:* `{', '.join(article['found_keywords'])}`\n📅 *Publicado em:* {date_str}\n🌐 *Fonte:* {article['source']}\n🔗 [Clique para ler]({article['link']})\n---")
        try:
            await context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            await asyncio.sleep(1.5)
        except TelegramError as e: logger.error(f"Falha ao enviar notificação: {e}")

async def monitor_loop(app: Application):
    context = ContextTypes.DEFAULT_TYPE(application=app)
    await asyncio.sleep(15)
    while True:
        config = load_config()
        if config.get("monitoring_on") and config.get("owner_id"):
            logger.info("Iniciando verificação automática.")
            await process_news(context)
        await asyncio.sleep(MONITORAMENTO_INTERVAL)

def is_owner(update: Update, config: dict): return update.effective_user.id == config.get("owner_id")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not config.get('owner_id'):
        config['owner_id'] = update.effective_user.id
        save_config(config)
        await update.message.reply_text("Bem-vindo! Use /menu. Em caso de problemas, use /limpar_tudo.")
    else: await update.message.reply_text("Bem-vindo de volta!")

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
            logger.info(f"Config removida pelo dono.")
        else: await msg.edit_text("ℹ️ Nenhuma configuração encontrada para apagar.")
    except Exception as e: await msg.edit_text(f"❌ Erro ao apagar: {e}"); logger.error(f"Falha ao apagar config: {e}")

async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query or update
    await query.message.reply_text("Iniciando verificação manual (últimos 7 dias)...")
    await process_news(context, is_manual=True, chat_id_manual=query.message.chat_id)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    await query.message.reply_text("Gerando status e diagnóstico (pode levar um momento)...")
    status_text = (f"📊 *Status e Diagnóstico*\n\n"
                   f"∙ Monitoramento: {'🟢 Ativo' if config.get('monitoring_on') else '🔴 Inativo'}\n"
                   f"∙ Palavras-chave: {len(config.get('keywords', []))}\n"
                   f"∙ Histórico (links salvos): {len(config.get('history', set()))}")
    keywords = config.get('keywords', [])
    if keywords:
        try:
            found_news = await fetch_news(keywords)
            total_found = len(found_news)
            if total_found > 0:
                relevant_count = sum(1 for article in found_news if any(k.lower() in f"{article['title']} {article['source']}".lower() for k in keywords))
                relevance_rate = (relevant_count / total_found) * 100 if total_found > 0 else 0
                status_text += (f"\n\n⚙️ *Performance da Busca (Simulação)*\n"
                                f"∙ Resultados brutos (Google): `{total_found}`\n"
                                f"∙ Notícias relevantes (filtradas): `{relevant_count}`\n"
                                f"∙ Taxa de relevância: `{relevance_rate:.1f}%`")
            else:
                status_text += "\n\n⚙️ *Performance da Busca (Simulação)*\n∙ A busca não retornou nenhum resultado."
        except Exception as e:
            logger.error(f"Erro na simulação de status: {e}")
            status_text += "\n\n⚙️ *Performance da Busca (Simulação)*\n∙ Ocorreu um erro ao tentar simular a busca."
    else:
        status_text += "\n\n⚙️ *Performance da Busca (Simulação)*\n∙ Adicione palavras-chave para testar a performance."
    if hasattr(query, 'message') and query.message:
        await query.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=status_text, parse_mode=ParseMode.MARKDOWN)

async def view_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    if not is_owner(update, config): return
    query = update.callback_query or update
    keywords = config.get('keywords', [])
    msg = f"📝 *Palavras-Chave ({len(keywords)}):*\n`{', '.join(keywords)}`" if keywords else "Nenhuma palavra-chave."
    await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Verificar Agora", callback_data='check_now')],
          [InlineKeyboardButton("Ligar/Desligar Monitoramento", callback_data='toggle_monitoring')],
          [InlineKeyboardButton("Ver Status e Diagnóstico", callback_data='status')],
          [InlineKeyboardButton("Listar Palavras-Chave", callback_data='view_keywords')]]
    await update.message.reply_text('⚙️ **Menu Principal**', reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

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
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"Monitoramento: {status_text}.")

async def post_init_task(app: Application): asyncio.create_task(monitor_loop(app))

def main():
    if not BOT_TOKEN: logger.error("ERRO: BOT_TOKEN não configurado!"); return
    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = post_init_task
    handlers = [CommandHandler('limpar_tudo', limpar_tudo), CommandHandler('start', start), CommandHandler('menu', menu_command),
                CommandHandler('status', status), CommandHandler('verificar', check_now), CommandHandler('verpalavras', view_keywords),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler), CallbackQueryHandler(button_handler)]
    app.add_handlers(handlers)
    logger.info("🚀 Faro Fino News v1.8 iniciando!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
