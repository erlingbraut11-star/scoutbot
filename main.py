import asyncio
import logging
import json
import aiohttp
from datetime import datetime
import anthropic
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "METS_TON_TOKEN_ICI"
ANTHROPIC_API_KEY = "METS_TA_CLE_ANTHROPIC_ICI"
API_FOOTBALL_KEY = "METS_TA_CLE_API_FOOTBALL_ICI"  # https://www.api-football.com
CHAT_ID = None
CONFIANCE_MINIMUM = 70

# Matchs déjà analysés pour éviter les doublons
matchs_analyses = set()
alertes_envoyees = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# RÉCUPÉRATION DES MATCHS EN DIRECT
# ============================================================
async def get_live_matches():
    """Récupère tous les matchs en cours via API-Football"""
    try:
        url = "https://v3.football.api-sports.io/fixtures?live=all"
        headers = {
            "x-apisports-key": API_FOOTBALL_KEY,
            "x-rapidapi-host": "v3.football.api-sports.io"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                matches = data.get("response", [])
                logger.info(f"🔴 {len(matches)} match(s) en direct trouvé(s)")
                return matches
    except Exception as e:
        logger.error(f"❌ Erreur API-Football: {e}")
        return []

def format_match_for_analysis(match):
    """Formate les données du match pour l'analyse Claude"""
    fixture = match.get("fixture", {})
    teams = match.get("teams", {})
    goals = match.get("goals", {})
    score = match.get("score", {})
    league = match.get("league", {})

    minute = fixture.get("status", {}).get("elapsed", 0) or 0
    home = teams.get("home", {}).get("name", "?")
    away = teams.get("away", {}).get("name", "?")
    home_goals = goals.get("home", 0) or 0
    away_goals = goals.get("away", 0) or 0
    halftime = score.get("halftime", {})

    return {
        "id": fixture.get("id"),
        "match": f"{home} vs {away}",
        "competition": league.get("name", "?"),
        "minute": minute,
        "score": f"{home_goals}-{away_goals}",
        "mi_temps": f"{halftime.get('home', '?')}-{halftime.get('away', '?')}",
        "home": home,
        "away": away,
        "home_goals": home_goals,
        "away_goals": away_goals,
    }

# ============================================================
# ANALYSE LIVE AVEC CLAUDE
# ============================================================
LIVE_PROMPT = """Tu es SCOUT LIVE, expert en paris sportifs en direct.

On te donne les données d'un match en cours. Analyse la situation et génère des opportunités de paris live.

Types de paris à analyser :
- Prochain buteur : quel joueur va marquer
- Over/Under buts : ex "Over 2.5 buts encore possible"
- Résultat final : qui va gagner le match
- Mi-temps/Full time : combo mi-temps + résultat final

Réponds UNIQUEMENT avec ce JSON :
{
  "match": "Équipe A vs Équipe B",
  "minute": 23,
  "score": "1-0",
  "analyse": "Analyse experte de la situation actuelle en 2-3 phrases",
  "opportunites": [
    {
      "type": "Prochain buteur" | "Over/Under" | "Résultat final" | "Mi-temps/Full time",
      "prediction": "ex: Over 2.5 buts | Victoire Équipe A | Mbappé prochain buteur",
      "raisonnement": "Pourquoi ce pari est intéressant maintenant",
      "cote_estimee": "1.85",
      "confiance": 75
    }
  ],
  "verdict": "Phrase de conclusion sur la situation du match"
}

Ne retourne QUE les opportunités avec confiance >= 70%.
Si aucune opportunité intéressante, retourne opportunites: []
JSON uniquement, aucun texte avant ou après."""

async def analyze_live_match(match_data):
    """Analyse un match en direct avec Claude + recherche web"""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        user_message = f"""Analyse ce match en direct :
Match : {match_data['match']}
Compétition : {match_data['competition']}
Minute : {match_data['minute']}'
Score actuel : {match_data['score']}
Score mi-temps : {match_data['mi_temps']}

Utilise web_search pour trouver :
- Les stats live du match (possession, tirs, corners, cartons)
- Les joueurs en forme / buteurs habituels
- Le contexte du match (enjeu, classement)

Puis génère les meilleures opportunités de paris live avec confiance >= 70%."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=LIVE_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_message}]
        )

        full_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                full_text += block.text

        clean = full_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)

        # Filtre les opportunités à 70%+
        result["opportunites"] = [
            o for o in result.get("opportunites", [])
            if o.get("confiance", 0) >= CONFIANCE_MINIMUM
        ]

        return result

    except Exception as e:
        logger.error(f"❌ Erreur analyse live: {e}")
        return None

# ============================================================
# FORMATAGE MESSAGE LIVE
# ============================================================
def format_live_alert(analysis, match_data):
    type_emoji = {
        "Prochain buteur": "⚡",
        "Over/Under": "📈",
        "Résultat final": "🏅",
        "Mi-temps/Full time": "🔄"
    }

    msg = f"🔴 *LIVE — {match_data['match']}*\n"
    msg += f"🏆 {match_data['competition']} · {match_data['minute']}'\n"
    msg += f"⚽ Score : *{match_data['score']}*\n\n"
    msg += f"📊 *Analyse :*\n{analysis.get('analyse', '')}\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "🎯 *OPPORTUNITÉS LIVE :*\n\n"

    for opp in analysis.get("opportunites", []):
        emoji = type_emoji.get(opp.get("type", ""), "🎯")
        confiance = opp.get("confiance", 0)
        stars = "🔥" if confiance >= 85 else "⭐"

        msg += f"{stars} {emoji} *{opp.get('type', '')}*\n"
        msg += f"   📌 {opp.get('prediction', '')}\n"
        msg += f"   💬 {opp.get('raisonnement', '')}\n"
        msg += f"   💶 Cote : {opp.get('cote_estimee', '?')} · 🎯 {confiance}%\n\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"_{analysis.get('verdict', '')}_\n\n"
    msg += "⚠️ _Pariez de manière responsable._"

    return msg

# ============================================================
# SCAN LIVE PRINCIPAL
# ============================================================
async def scan_live_matches(bot=None, chat_id=None):
    """Scanne tous les matchs en direct et envoie les alertes"""
    target_chat = chat_id or CHAT_ID
    if not target_chat or not bot:
        logger.warning("⚠️ Chat ID non défini.")
        return

    logger.info("🔴 Scan des matchs en direct...")
    matches = await get_live_matches()

    if not matches:
        logger.info("Aucun match en direct.")
        return

    for match in matches:
        match_data = format_match_for_analysis(match)
        match_id = match_data["id"]
        minute = match_data["minute"]

        # Évite d'analyser le même match trop souvent (toutes les 15 min)
        last_alert = alertes_envoyees.get(match_id, 0)
        if minute - last_alert < 15 and match_id in alertes_envoyees:
            continue

        logger.info(f"🔍 Analyse : {match_data['match']} ({minute}')")

        analysis = await analyze_live_match(match_data)

        if analysis and analysis.get("opportunites"):
            message = format_live_alert(analysis, match_data)
            try:
                await bot.send_message(
                    chat_id=target_chat,
                    text=message,
                    parse_mode="Markdown"
                )
                alertes_envoyees[match_id] = minute
                logger.info(f"✅ Alerte envoyée : {match_data['match']}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi: {e}")

        # Pause entre chaque analyse pour ne pas surcharger l'API
        await asyncio.sleep(5)

# ============================================================
# COMMANDES TELEGRAM
# ============================================================
async def start_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "🔴 *SCOUT LIVE est en ligne !*\n\n"
        "Je surveille tous les matchs en direct et t'envoie des alertes paris dès qu'une opportunité ≥ 70% est détectée.\n\n"
        "⚽ Football · 🏀 Basketball · 🎾 Tennis\n\n"
        "📋 *Commandes :*\n"
        "/live — Scanner les matchs en cours maintenant\n"
        "/status — Vérifier que SCOUT LIVE fonctionne\n\n"
        f"🎯 Seuil de confiance : {CONFIANCE_MINIMUM}%\n\n"
        "🔴 Surveillance automatique toutes les 10 minutes !",
        parse_mode="Markdown"
    )

async def live_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "🔍 *Scan des matchs en direct...*\n_Patiente 30 secondes !_",
        parse_mode="Markdown"
    )
    await scan_live_matches(bot=context.bot, chat_id=CHAT_ID)

async def status_command(update, context):
    nb_matchs = len(alertes_envoyees)
    await update.message.reply_text(
        "✅ *SCOUT LIVE est opérationnel !*\n\n"
        f"🔴 Scan automatique : toutes les 10 minutes\n"
        f"🎯 Seuil minimum : {CONFIANCE_MINIMUM}%\n"
        f"📊 Matchs analysés cette session : {nb_matchs}\n\n"
        "⚽ Football · 🏀 Basketball · 🎾 Tennis",
        parse_mode="Markdown"
    )

async def message_handler(update, context):
    await update.message.reply_text(
        "Utilise /live pour scanner les matchs en cours ! 🔴"
    )

# ============================================================
# PLANIFICATEUR — Scan toutes les 10 minutes
# ============================================================
async def post_init(application):
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(
        scan_live_matches,
        trigger="interval",
        minutes=10,
        kwargs={"bot": application.bot, "chat_id": CHAT_ID}
    )
    scheduler.start()
    logger.info("⏰ Scan live toutes les 10 minutes")

def main():
    logger.info("🚀 Démarrage de SCOUT LIVE...")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("✅ SCOUT LIVE est en ligne !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
