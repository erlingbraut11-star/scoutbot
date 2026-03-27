import asyncio
import logging
import json
import re
from datetime import datetime
import anthropic
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# CONFIGURATION — Remplace par tes vraies clés
# ============================================================
TELEGRAM_TOKEN = "8676839563:AAEfKKKJebVUrfg5ab6BBChZsmAtdPwer7Y"
ANTHROPIC_API_KEY = "METS_TA_CLE_ANTHROPIC_ICI"  # sk-ant-...
CHAT_ID = None  # Sera auto-détecté au premier /start

# ============================================================
# SYSTEM PROMPT SCOUT
# ============================================================
SYSTEM_PROMPT = """Tu es SCOUT, expert analyste en paris sportifs.

ÉTAPE 1 — Utilise web_search pour chercher les matchs de ce soir dans ces compétitions :
- Ligue des Champions UEFA
- Ligue 1, Premier League, Liga, Serie A, Bundesliga
- Matchs internationaux (Équipes nationales, qualifications Coupe du Monde, UEFA Nations League, CONMEBOL, matchs amicaux internationaux)
- NBA
- ATP/WTA Grand Chelem et Masters

ÉTAPE 2 — Pour chaque match trouvé, recherche : effectifs actuels, blessés, forme récente (5 derniers matchs), confrontations directes.

ÉTAPE 3 — Analyse chaque match et génère des pronostics. Ne retourne QUE les matchs avec une confiance >= 60%.

Pour chaque match, génère jusqu'à 3 pronostics de types différents :
- 1N2 : résultat du match
- Buteur/Scoreur : joueur qui va marquer
- Over/Under : ex "Over 2.5 buts" ou "Under 2.5 buts" (foot), "Over 220.5 points" (basket)

Réponds UNIQUEMENT avec ce JSON (tableau de pronostics) :
[
  {
    "match": "Équipe A vs Équipe B",
    "sport": "Football" | "Basketball" | "Tennis",
    "competition": "Nom compétition",
    "date": "ce soir HH:MM",
    "analyse": "Analyse experte 2-3 phrases avec stats récentes",
    "pronostics": [
      {
        "type": "1N2",
        "prediction": "ex: Victoire Équipe A (1)",
        "cote_estimee": "1.75",
        "confiance": 85
      },
      {
        "type": "Over/Under",
        "prediction": "Over 2.5 buts",
        "cote_estimee": "1.85",
        "confiance": 75
      },
      {
        "type": "Buteur/Scoreur",
        "prediction": "Mbappé Buteur",
        "cote_estimee": "2.10",
        "confiance": 72
      }
    ],
    "stats_cles": ["stat 1", "stat 2"],
    "blesses": ["Joueur blessé si pertinent"],
    "verdict": "Phrase de conclusion"
  }
]

Si aucun match n'atteint 60% de confiance, retourne un tableau vide : []
IMPORTANT: JSON uniquement, aucun texte avant ou après."""

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# ANALYSE SCOUT VIA API ANTHROPIC
# ============================================================
async def run_scout_analysis():
    """Lance l'analyse SCOUT et retourne les pronostics >= 60%"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("🔍 SCOUT lance l'analyse des matchs de ce soir...")

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": "Analyse les matchs de ce soir et donne-moi les pronostics avec 60%+ de confiance."}]
        )

        full_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text

        clean = full_text.replace("```json", "").replace("```", "").strip()
        pronostics = json.loads(clean)
        def max_confiance(p):
            pronostics = p.get("pronostics", [p.get("pronostic", {})]) 
            return max((pr.get("confiance", 0) for pr in pronostics), default=0)
        
        filtered = [p for p in pronostics if max_confiance(p) >= 60]
        logger.info(f"✅ {len(filtered)} pronostic(s) >= 60% trouvé(s)")
        return filtered

    except Exception as e:
        logger.error(f"❌ Erreur analyse SCOUT: {e}")
        return []

# ============================================================
# FORMATAGE DU MESSAGE TELEGRAM
# ============================================================
def format_pronostic(p):
    sport_emoji = {"Football": "⚽", "Basketball": "🏀", "Tennis": "🎾"}.get(p.get("sport"), "🏆")
    type_emoji = {"1N2": "🏅", "Over/Under": "📈", "Buteur/Scoreur": "⚡"}
    
    msg = f"{sport_emoji} *{p.get('match', '')}*\n"
    msg += f"🏆 {p.get('competition', '')} · {p.get('date', 'ce soir')}\n\n"
    msg += f"📊 *Analyse :*\n{p.get('analyse', '')}\n\n"
    
    if p.get("blesses"):
        msg += f"🚑 *Absents :* {', '.join(p['blesses'])}\n\n"
    
    msg += f"📌 *Stats clés :*\n"
    for s in p.get("stats_cles", []):
        msg += f"  • {s}\n"
    
    msg += "\n"
    
    # Gère les nouveaux pronostics multiples ET l'ancien format
    pronostics = p.get("pronostics", [])
    if not pronostics and p.get("pronostic"):
        pronostics = [p.get("pronostic")]
    
    for prono in pronostics:
        confiance = prono.get("confiance", 0)
        stars = "🔥" if confiance >= 85 else "⭐"
        emoji = type_emoji.get(prono.get("type", ""), "🎯")
        msg += f"{stars} {emoji} *{prono.get('type', '')} :* {prono.get('prediction', '')}\n"
        msg += f"   💶 Cote : {prono.get('cote_estimee', '?')} · 🎯 Confiance : {confiance}%\n\n"
    
    msg += f"_{p.get('verdict', '')}_"
    return msg

def format_daily_message(pronostics):
    now = datetime.now().strftime("%d/%m/%Y")
    
    if not pronostics:
        return (
            "🏆 *SCOUT — Analyse du jour*\n"
            f"📅 {now}\n\n"
            "Aucun match n'atteint le seuil de confiance de 60% aujourd'hui.\n"
            "SCOUT reste prudent — pas de prono forcé ! 🛡️"
        )
    
    header = (
        f"🏆 *SCOUT — Pronostics du {now}*\n"
        f"✅ *{len(pronostics)} pronostic(s) sélectionné(s) à 60%+*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    body = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n".join(
        format_pronostic(p) for p in pronostics
    )
    
    footer = (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ _Pariez de manière responsable. SCOUT est un outil d'aide à la décision._"
    )
    
    return header + body + footer

# ============================================================
# ENVOI TELEGRAM
# ============================================================
async def send_daily_pronostics(context: ContextTypes.DEFAULT_TYPE = None, bot: Bot = None, chat_id: str = None):
    """Tâche planifiée : analyse + envoi des pronostics"""
    target_chat = chat_id or CHAT_ID
    target_bot = bot or (context.bot if context else None)
    
    if not target_chat or not target_bot:
        logger.warning("⚠️ Chat ID non défini. Envoie /start au bot d'abord.")
        return
    
    try:
        await target_bot.send_message(
            chat_id=target_chat,
            text="🔍 *SCOUT analyse les matchs de ce soir...*\n_Recherche des effectifs, blessés, forme récente — patiente 30 secondes !_",
            parse_mode="Markdown"
        )
        
        pronostics = await run_scout_analysis()
        message = format_daily_message(pronostics)
        
        # Découpe si message trop long
        if len(message) > 4000:
            chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
            for chunk in chunks:
                await target_bot.send_message(chat_id=target_chat, text=chunk, parse_mode="Markdown")
        else:
            await target_bot.send_message(chat_id=target_chat, text=message, parse_mode="Markdown")
            
        logger.info(f"✅ Pronostics envoyés à {target_chat}")
        
    except Exception as e:
        logger.error(f"❌ Erreur envoi: {e}")
        if target_bot and target_chat:
            await target_bot.send_message(
                chat_id=target_chat,
                text=f"❌ Erreur SCOUT: {str(e)}\nRéessaie avec /analyse"
            )

# ============================================================
# COMMANDES TELEGRAM
# ============================================================
async def start_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    logger.info(f"✅ Chat ID enregistré: {CHAT_ID}")
    
    await update.message.reply_text(
        "🏆 *SCOUT est en ligne !*\n\n"
        "Je t'enverrai automatiquement les meilleurs pronostics chaque soir à *18h00*.\n\n"
        "📋 *Commandes disponibles :*\n"
        "/analyse — Lancer une analyse immédiate\n"
        "/status — Vérifier que SCOUT fonctionne\n"
        "/aide — Afficher l'aide\n\n"
        f"🆔 _Ton Chat ID : `{CHAT_ID}`_\n\n"
        "⚽🏀🎾 Prêt à gagner !",
        parse_mode="Markdown"
    )

async def analyse_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await send_daily_pronostics(bot=context.bot, chat_id=CHAT_ID)

async def status_command(update, context):
    await update.message.reply_text(
        "✅ *SCOUT est opérationnel !*\n\n"
        "🕕 Envoi automatique : chaque jour à *18h00*\n"
        "🎯 Seuil minimum : *60% de confiance*\n"
        "🌐 Données : temps réel via recherche web\n\n"
        "⚽ Foot · 🏀 Basket · 🎾 Tennis",
        parse_mode="Markdown"
    )

async def aide_command(update, context):
    await update.message.reply_text(
        "📋 *Aide SCOUT*\n\n"
        "/start — Démarrer et enregistrer ce chat\n"
        "/analyse — Analyser les matchs de ce soir maintenant\n"
        "/status — Vérifier le statut du bot\n"
        "/aide — Afficher ce message\n\n"
        "🕕 *Envoi automatique :* 18h00 chaque jour\n"
        "🎯 *Filtre :* uniquement les pronostics ≥ 60%\n\n"
        "⚠️ _Pariez de manière responsable._",
        parse_mode="Markdown"
    )

async def message_handler(update, context):
    await update.message.reply_text(
        "Utilise /analyse pour lancer une analyse, ou /aide pour voir les commandes disponibles. 🏆"
    )

# ============================================================
# LANCEMENT DU BOT
# ============================================================
async def post_init(application):
    """Démarre le planificateur après l'initialisation de l'app"""
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(
        send_daily_pronostics,
        trigger="cron",
        hour=18,
        minute=0,
        kwargs={"bot": application.bot, "chat_id": CHAT_ID}
    )
    scheduler.start()
    logger.info("⏰ Planificateur démarré — envoi à 18h00 chaque jour")

def main():
    logger.info("🚀 Démarrage de SCOUT Bot...")
    
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Commandes
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("analyse", analyse_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("aide", aide_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("✅ SCOUT Bot est en ligne !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
