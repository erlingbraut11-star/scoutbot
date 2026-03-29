import os
import asyncio
import logging
import json
import aiohttp
from datetime import datetime
import anthropic
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
CHAT_ID = None

alertes_envoyees = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# PROMPT SCOUT
# ============================================================

SYSTEM_PROMPT = """Tu es SCOUT, expert analyste en paris sportifs.

ÉTAPE 1 — Utilise web_search pour chercher les matchs d'aujourd'hui et de demain dans ces compétitions :
- Ligue des Champions UEFA
- Ligue 1, Premier League, Liga, Serie A, Bundesliga
- Matchs internationaux (qualifications Coupe du Monde, UEFA Nations League, matchs amicaux)
- NBA
- ATP/WTA Grand Chelem et Masters

ÉTAPE 2 — Pour chaque match trouvé, recherche : effectifs actuels, blessés, forme récente (5 derniers matchs), confrontations directes, stats offensives/défensives.

ÉTAPE 3 — Analyse et sélectionne les 3 à 5 matchs les plus intéressants à parier. Génère pour chacun jusqu'à 2 pronostics solides.

Pour chaque pronostic :
- 1N2 : résultat du match
- Over/Under buts/points
- Buteur/Scoreur : joueur probable

Réponds UNIQUEMENT avec ce JSON, sans texte avant ou après :
[
  {
    "match": "Équipe A vs Équipe B",
    "sport": "Football",
    "competition": "Nom compétition",
    "date": "Aujourd'hui/Demain HH:MM",
    "analyse": "Analyse experte 2-3 phrases avec stats récentes et contexte",
    "pronostics": [
      {
        "type": "1N2",
        "prediction": "Victoire Équipe A",
        "cote_estimee": "1.75",
        "confiance": 82
      },
      {
        "type": "Over/Under",
        "prediction": "Over 2.5 buts",
        "cote_estimee": "1.90",
        "confiance": 74
      }
    ],
    "stats_cles": ["Équipe A : 4 victoires sur les 5 derniers matchs", "Équipe B : 3 blessés majeurs"],
    "blesses": ["Nom joueur blessé si pertinent"],
    "verdict": "Phrase de conclusion sur le meilleur pari"
  }
]"""


async def run_scout_analysis():
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("🔍 SCOUT analyse les matchs...")

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": "Analyse les matchs d'aujourd'hui et de demain. Donne-moi les meilleurs pronostics."}]
        )

        full_text = "".join(b.text for b in response.content if hasattr(b, "text"))
        clean = full_text.replace("```json", "").replace("```", "").strip()
        pronostics = json.loads(clean)
        logger.info(f"✅ {len(pronostics)} pronostic(s) générés")
        return pronostics

    except Exception as e:
        logger.error(f"❌ Erreur analyse SCOUT: {e}")
        return []


def format_pronostic(p):
    sport_emoji = {"Football": "⚽", "Basketball": "🏀", "Tennis": "🎾"}.get(p.get("sport"), "🏆")
    type_emoji = {"1N2": "🏅", "Over/Under": "📈", "Buteur/Scoreur": "⚡"}

    msg = f"{sport_emoji} *{p.get('match', '')}*\n"
    msg += f"🏆 {p.get('competition', '')} · {p.get('date', '')}\n\n"
    msg += f"📊 *Analyse :*\n{p.get('analyse', '')}\n\n"

    if p.get("blesses"):
        msg += f"🚑 *Absents :* {', '.join(p['blesses'])}\n\n"

    msg += "📌 *Stats clés :*\n"
    for s in p.get("stats_cles", []):
        msg += f"  • {s}\n"
    msg += "\n"

    for prono in p.get("pronostics", []):
        confiance = prono.get("confiance", 0)
        stars = "🔥" if confiance >= 80 else "⭐"
        emoji = type_emoji.get(prono.get("type", ""), "🎯")
        msg += f"{stars} {emoji} *{prono.get('type', '')} :* {prono.get('prediction', '')}\n"
        msg += f"   💶 Cote : {prono.get('cote_estimee', '?')} · 🎯 Confiance : {confiance}%\n\n"

    msg += f"_{p.get('verdict', '')}_"
    return msg


def format_message(pronostics):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    if not pronostics:
        return (
            "🏆 *SCOUT — Analyse terminée*\n"
            f"📅 {now}\n\n"
            "Aucun match intéressant trouvé pour le moment.\nRéessaie plus tard ! 🛡️"
        )
    header = (
        f"🏆 *SCOUT — Pronostics du {now}*\n"
        f"✅ *{len(pronostics)} match(s) analysé(s)*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    body = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n".join(format_pronostic(p) for p in pronostics)
    footer = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n⚠️ _Pariez de manière responsable._"
    return header + body + footer


# ============================================================
# ====================== SCOUT LIVE ==========================
# ============================================================

LIVE_PROMPT = """Tu es SCOUT LIVE, expert en paris sportifs en direct.

Analyse un match en cours et génère des opportunités de paris live.

Types de paris :
- Prochain buteur
- Over/Under buts
- Résultat final
- Mi-temps/Full time

Réponds UNIQUEMENT avec ce JSON :
{
  "match": "Équipe A vs Équipe B",
  "minute": 23,
  "score": "1-0",
  "analyse": "Analyse de la situation actuelle en 2-3 phrases",
  "opportunites": [
    {
      "type": "Prochain buteur",
      "prediction": "ex: Over 2.5 buts",
      "raisonnement": "Pourquoi ce pari est intéressant maintenant",
      "cote_estimee": "1.85",
      "confiance": 75
    }
  ],
  "verdict": "Phrase de conclusion"
}
JSON uniquement, aucun texte avant ou après."""


async def get_live_matches():
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures?live=all"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()
                matches = data.get("response", [])
                logger.info(f"🔴 {len(matches)} match(s) en direct")
                return matches
    except Exception as e:
        logger.error(f"❌ Erreur API-Football: {e}")
        return []


def format_match_data(match):
    fixture = match.get("fixture", {})
    teams = match.get("teams", {})
    goals = match.get("goals", {})
    score = match.get("score", {})
    league = match.get("league", {})
    halftime = score.get("halftime", {})
    return {
        "id": fixture.get("id"),
        "match": f"{teams.get('home', {}).get('name', '?')} vs {teams.get('away', {}).get('name', '?')}",
        "competition": league.get("name", "?"),
        "minute": fixture.get("status", {}).get("elapsed", 0) or 0,
        "score": f"{goals.get('home', 0) or 0}-{goals.get('away', 0) or 0}",
        "mi_temps": f"{halftime.get('home', '?')}-{halftime.get('away', '?')}",
    }


async def analyze_live_match(match_data):
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        user_msg = f"""Match en direct :
Match : {match_data['match']}
Compétition : {match_data['competition']}
Minute : {match_data['minute']}'
Score : {match_data['score']}
Mi-temps : {match_data['mi_temps']}

Utilise web_search pour trouver les stats live (possession, tirs, corners, cartons) et génère les meilleures opportunités de paris."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=LIVE_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_msg}]
        )
        full_text = "".join(b.text for b in response.content if hasattr(b, "text"))
        clean = full_text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception as e:
        logger.error(f"❌ Erreur analyse live: {e}")
        return None


def format_live_alert(analysis, match_data):
    type_emoji = {"Prochain buteur": "⚡", "Over/Under": "📈", "Résultat final": "🏅", "Mi-temps/Full time": "🔄"}
    msg = f"🔴 *LIVE — {match_data['match']}*\n"
    msg += f"🏆 {match_data['competition']} · {match_data['minute']}'\n"
    msg += f"⚽ Score : *{match_data['score']}*\n\n"
    msg += f"📊 *Analyse :*\n{analysis.get('analyse', '')}\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━\n🎯 *OPPORTUNITÉS LIVE :*\n\n"
    for opp in analysis.get("opportunites", []):
        emoji = type_emoji.get(opp.get("type", ""), "🎯")
        confiance = opp.get("confiance", 0)
        stars = "🔥" if confiance >= 80 else "⭐"
        msg += f"{stars} {emoji} *{opp.get('type', '')}*\n"
        msg += f"   📌 {opp.get('prediction', '')}\n"
        msg += f"   💬 {opp.get('raisonnement', '')}\n"
        msg += f"   💶 Cote : {opp.get('cote_estimee', '?')} · 🎯 {confiance}%\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━\n_{analysis.get('verdict', '')}_\n\n"
    msg += "⚠️ _Pariez de manière responsable._"
    return msg


async def scan_live_matches(bot=None, chat_id=None):
    target_chat = chat_id or CHAT_ID
    if not target_chat or not bot:
        return
    matches = await get_live_matches()
    if not matches:
        return
    for match in matches:
        match_data = format_match_data(match)
        match_id = match_data["id"]
        minute = match_data["minute"]
        last_alert = alertes_envoyees.get(match_id, -15)
        if minute - last_alert < 15:
            continue
        logger.info(f"🔍 Analyse live : {match_data['match']} ({minute}')")
        analysis = await analyze_live_match(match_data)
        if analysis and analysis.get("opportunites"):
            try:
                message = format_live_alert(analysis, match_data)
                await bot.send_message(chat_id=target_chat, text=message, parse_mode="Markdown")
                alertes_envoyees[match_id] = minute
                logger.info(f"✅ Alerte live envoyée : {match_data['match']}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi live: {e}")
        await asyncio.sleep(5)


# ============================================================
# =================== COMMANDES TELEGRAM =====================
# ============================================================

async def start_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "🏆 *SCOUT est en ligne !*\n\n"
        "📋 *Commandes :*\n"
        "/prono — Meilleurs pronostics du moment\n"
        "/live — Scanner les matchs en direct\n"
        "/status — Vérifier que SCOUT fonctionne\n"
        "/aide — Afficher l'aide\n\n"
        "⚽🏀🎾 Prêt à analyser !",
        parse_mode="Markdown"
    )


async def prono_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "🔍 *SCOUT analyse les matchs...*\n_Patiente 30 secondes !_",
        parse_mode="Markdown"
    )
    pronostics = await run_scout_analysis()
    message = format_message(pronostics)
    if len(message) > 4000:
        for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(message, parse_mode="Markdown")


async def live_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await update.message.reply_text(
        "🔴 *Scan des matchs en direct...*\n_Patiente 30 secondes !_",
        parse_mode="Markdown"
    )
    await scan_live_matches(bot=context.bot, chat_id=CHAT_ID)


async def status_command(update, context):
    await update.message.reply_text(
        "✅ *SCOUT est opérationnel !*\n\n"
        "🔴 Live betting : *toutes les 10 minutes*\n"
        "🌐 Données : temps réel\n\n"
        "⚽ Foot · 🏀 Basket · 🎾 Tennis",
        parse_mode="Markdown"
    )


async def aide_command(update, context):
    await update.message.reply_text(
        "📋 *Aide SCOUT*\n\n"
        "/start — Démarrer SCOUT\n"
        "/prono — Meilleurs pronostics du moment\n"
        "/live — Scanner les matchs en direct\n"
        "/status — Vérifier le statut\n"
        "/aide — Ce message\n\n"
        "⚠️ _Pariez de manière responsable._",
        parse_mode="Markdown"
    )


async def message_handler(update, context):
    await update.message.reply_text(
        "Utilise /prono pour les pronostics ou /live pour le direct ! 🏆"
    )


# ============================================================
# =================== PLANIFICATEUR ==========================
# ============================================================

async def post_init(application):
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")

    # Scan live toutes les 10 minutes
    scheduler.add_job(
        scan_live_matches,
        trigger="interval",
        minutes=10,
        kwargs={"bot": application.bot, "chat_id": CHAT_ID}
    )

    scheduler.start()
    logger.info("⏰ Planificateur : scan live toutes les 10 min")


def main():
    logger.info("🚀 Démarrage de SCOUT...")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("prono", prono_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("aide", aide_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("✅ SCOUT est en ligne !")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
