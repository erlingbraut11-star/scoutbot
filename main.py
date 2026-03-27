import asyncio
import logging
import json
import aiohttp
from datetime import datetime
import anthropic
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = "METS_TON_TOKEN_ICI"
ANTHROPIC_API_KEY = "METS_TA_CLE_ANTHROPIC_ICI"
API_FOOTBALL_KEY = "METS_TA_CLE_API_FOOTBALL_ICI"
CHAT_ID = None
CONFIANCE_MINIMUM = 70

# Suivi des alertes live pour éviter les doublons
alertes_envoyees = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# =================== SCOUT CLASSIQUE ========================
# ============================================================

SYSTEM_PROMPT = """Tu es SCOUT, expert analyste en paris sportifs.

ÉTAPE 1 — Utilise web_search pour chercher les matchs de demain dans ces compétitions :
- Ligue des Champions UEFA
- Ligue 1, Premier League, Liga, Serie A, Bundesliga
- Matchs internationaux (Équipes nationales, qualifications Coupe du Monde, UEFA Nations League, CONMEBOL, matchs amicaux internationaux)
- NBA
- ATP/WTA Grand Chelem et Masters

ÉTAPE 2 — Pour chaque match trouvé, recherche : effectifs actuels, blessés, forme récente (5 derniers matchs), confrontations directes.

ÉTAPE 3 — Analyse chaque match et génère des pronostics. Ne retourne QUE les matchs avec une confiance >= 70%.

Pour chaque match, génère jusqu'à 3 pronostics de types différents :
- 1N2 : résultat du match
- Buteur/Scoreur : joueur qui va marquer
- Over/Under : ex "Over 2.5 buts" ou "Under 2.5 buts" (foot), "Over 220.5 points" (basket)

Réponds UNIQUEMENT avec ce JSON :
[
  {
    "match": "Équipe A vs Équipe B",
    "sport": "Football" | "Basketball" | "Tennis",
    "competition": "Nom compétition",
    "date": "Demain HH:MM",
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

Si aucun match n'atteint 70% de confiance, retourne : []
JSON uniquement, aucun texte avant ou après."""

async def run_scout_analysis():
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        logger.info("🔍 SCOUT analyse les matchs de demain...")
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": "Analyse les matchs de demain et donne-moi les pronostics avec 70%+ de confiance."}]
        )
        full_text = "".join(b.text for b in response.content if hasattr(b, "text"))
        clean = full_text.replace("```json", "").replace("```", "").strip()
        pronostics = json.loads(clean)

        def max_confiance(p):
            pros = p.get("pronostics", [p.get("pronostic", {})])
            return max((pr.get("confiance", 0) for pr in pros), default=0)

        filtered = [p for p in pronostics if max_confiance(p) >= CONFIANCE_MINIMUM]
        logger.info(f"✅ {len(filtered)} pronostic(s) >= {CONFIANCE_MINIMUM}% trouvé(s)")
        return filtered
    except Exception as e:
        logger.error(f"❌ Erreur analyse SCOUT: {e}")
        return []

def format_pronostic(p):
    sport_emoji = {"Football": "⚽", "Basketball": "🏀", "Tennis": "🎾"}.get(p.get("sport"), "🏆")
    type_emoji = {"1N2": "🏅", "Over/Under": "📈", "Buteur/Scoreur": "⚡"}
    msg = f"{sport_emoji} *{p.get('match', '')}*\n"
    msg += f"🏆 {p.get('competition', '')} · {p.get('date', 'Demain')}\n\n"
    msg += f"📊 *Analyse :*\n{p.get('analyse', '')}\n\n"
    if p.get("blesses"):
        msg += f"🚑 *Absents :* {', '.join(p['blesses'])}\n\n"
    msg += "📌 *Stats clés :*\n"
    for s in p.get("stats_cles", []):
        msg += f"  • {s}\n"
    msg += "\n"
    pronostics = p.get("pronostics", [])
    if not pronostics and p.get("pronostic"):
        pronostics = [p.get("pronostic")]
    for prono in pronostics:
        confiance = prono.get("confiance", 0)
        stars = "🔥" if confiance >= 85 else "⭐"
        emoji = type_emoji.get(prono.get("type", ""), "🎯")
        msg += f"{stars} {emoji} *{prono.get('type', '')} :* {prono.get('prediction', '')}\n"
        msg += f"   💶 Cote : {prono.get('cote_estimee', '?')} · 🎯 {confiance}%\n\n"
    msg += f"_{p.get('verdict', '')}_"
    return msg

def format_daily_message(pronostics):
    now = datetime.now().strftime("%d/%m/%Y")
    if not pronostics:
        return (
            "🏆 *SCOUT — Analyse du jour*\n"
            f"📅 {now}\n\n"
            f"Aucun match n'atteint le seuil de {CONFIANCE_MINIMUM}% aujourd'hui.\n"
            "SCOUT reste prudent — pas de prono forcé ! 🛡️"
        )
    header = (
        f"🏆 *SCOUT — Pronostics du {now}*\n"
        f"✅ *{len(pronostics)} pronostic(s) sélectionné(s) à {CONFIANCE_MINIMUM}%+*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    body = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n".join(format_pronostic(p) for p in pronostics)
    footer = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n⚠️ _Pariez de manière responsable._"
    return header + body + footer

async def send_daily_pronostics(bot=None, chat_id=None):
    target_chat = chat_id or CHAT_ID
    if not target_chat or not bot:
        return
    try:
        await bot.send_message(
            chat_id=target_chat,
            text="🔍 *SCOUT analyse les matchs de demain...*\n_Patiente 30 secondes !_",
            parse_mode="Markdown"
        )
        pronostics = await run_scout_analysis()
        message = format_daily_message(pronostics)
        if len(message) > 4000:
            for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
                await bot.send_message(chat_id=target_chat, text=chunk, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=target_chat, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"❌ Erreur envoi pronostics: {e}")

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
      "type": "Prochain buteur" | "Over/Under" | "Résultat final" | "Mi-temps/Full time",
      "prediction": "ex: Over 2.5 buts",
      "raisonnement": "Pourquoi ce pari est intéressant maintenant",
      "cote_estimee": "1.85",
      "confiance": 75
    }
  ],
  "verdict": "Phrase de conclusion"
}

Ne retourne QUE les opportunités avec confiance >= 70%.
Si aucune opportunité, retourne opportunites: []
JSON uniquement."""

async def get_live_matches():
    try:
        url = "https://v3.football.api-sports.io/fixtures?live=all"
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
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

Utilise web_search pour trouver les stats live (possession, tirs, corners, cartons) et génère les opportunités de paris ≥ 70%."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=LIVE_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_msg}]
        )
        full_text = "".join(b.text for b in response.content if hasattr(b, "text"))
        clean = full_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        result["opportunites"] = [o for o in result.get("opportunites", []) if o.get("confiance", 0) >= CONFIANCE_MINIMUM]
        return result
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
        stars = "🔥" if confiance >= 85 else "⭐"
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
        "📅 *Pronostics du jour :* envoi automatique à *18h00*\n"
        "🔴 *Live betting :* surveillance toutes les *10 minutes*\n\n"
        "📋 *Commandes :*\n"
        "/analyse — Pronostics de demain maintenant\n"
        "/live — Scanner les matchs en direct maintenant\n"
        "/status — Vérifier que SCOUT fonctionne\n"
        "/aide — Afficher l'aide\n\n"
        f"🎯 Seuil de confiance : {CONFIANCE_MINIMUM}%\n"
        "⚽🏀🎾 Prêt à gagner !",
        parse_mode="Markdown"
    )

async def analyse_command(update, context):
    global CHAT_ID
    CHAT_ID = str(update.effective_chat.id)
    await send_daily_pronostics(bot=context.bot, chat_id=CHAT_ID)

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
        "📅 Pronostics : *18h00 chaque jour*\n"
        "🔴 Live betting : *toutes les 10 minutes*\n"
        f"🎯 Seuil : *{CONFIANCE_MINIMUM}%*\n"
        "🌐 Données : temps réel\n\n"
        "⚽ Foot · 🏀 Basket · 🎾 Tennis",
        parse_mode="Markdown"
    )

async def aide_command(update, context):
    await update.message.reply_text(
        "📋 *Aide SCOUT*\n\n"
        "/start — Démarrer SCOUT\n"
        "/analyse — Pronostics des matchs de demain\n"
        "/live — Scanner les matchs en direct\n"
        "/status — Vérifier le statut\n"
        "/aide — Ce message\n\n"
        "📅 *Automatique :* pronostics à 18h + live toutes les 10 min\n"
        f"🎯 *Seuil :* {CONFIANCE_MINIMUM}%\n\n"
        "⚠️ _Pariez de manière responsable._",
        parse_mode="Markdown"
    )

async def message_handler(update, context):
    await update.message.reply_text(
        "Utilise /analyse pour les pronostics ou /live pour le direct ! 🏆"
    )

# ============================================================
# =================== PLANIFICATEUR ==========================
# ============================================================

async def post_init(application):
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")

    # Pronostics quotidiens à 18h
    scheduler.add_job(
        send_daily_pronostics,
        trigger="cron",
        hour=18,
        minute=0,
        kwargs={"bot": application.bot, "chat_id": CHAT_ID}
    )

    # Scan live toutes les 10 minutes
    scheduler.add_job(
        scan_live_matches,
        trigger="interval",
        minutes=10,
        kwargs={"bot": application.bot, "chat_id": CHAT_ID}
    )

    scheduler.start()
    logger.info("⏰ Planificateur : pronostics 18h + live toutes les 10 min")

def main():
    logger.info("🚀 Démarrage de SCOUT...")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("analyse", analyse_command))
    app.add_handler(CommandHandler("live", live_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("aide", aide_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("✅ SCOUT est en ligne !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
