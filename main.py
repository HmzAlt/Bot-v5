import os
import json
import asyncio
from datetime import datetime, date, timedelta
from typing import Dict, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dateutil import parser
import pytz

from webserver import keep_alive
import os
# [...]
keep_alive()
client.run(os.environ["DISCORD_TOKEN"])

from flask import Flask
from threading import Thread

app = Flask("keepalive")

@app.route("/")
def home():
    return "Alive"

def keep_alive():
    port = int(os.environ.get("PORT", 8080))  # Use Replit port or 8080
    Thread(target=lambda: app.run(host="0.0.0.0", port=port)).start()

# Config / storage
GUILDS_FILE = "guilds.json"
ALADHAN_BY_CITY_URL = "http://api.aladhan.com/v1/timingsByCity"

CITY = "London"
COUNTRY = "United Kingdom"
CALC_METHOD = 2  # University of Islamic Sciences, Karachi

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

def load_guilds() -> Dict[str, Any]:
    if not os.path.exists(GUILDS_FILE):
        return {}
    with open(GUILDS_FILE, "r") as f:
        return json.load(f)

def save_guilds(data: Dict[str, Any]):
    with open(GUILDS_FILE, "w") as f:
        json.dump(data, f, indent=2)

guilds = load_guilds()

async def fetch_prayer_times(session: aiohttp.ClientSession, target_date: date):
    params = {
        "city": CITY,
        "country": COUNTRY,
        "method": CALC_METHOD,
        "date": target_date.strftime("%d-%m-%Y"),
    }
    async with session.get(ALADHAN_BY_CITY_URL, params=params) as resp:
        data = await resp.json()
        if data.get("code") != 200:
            raise RuntimeError("AlAdhan API error: " + str(data))
        return data["data"]

def build_prayer_datetimes(api_data):
    tz_name = api_data.get("meta", {}).get("timezone") or "Europe/London"
    timezone = pytz.timezone(tz_name)
    timings = api_data["timings"]

    greg = api_data["date"]["gregorian"]["date"]
    day, month, year = [int(x) for x in greg.split("-")]

    prayer_dt = {}
    for name in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
        tstr = timings.get(name)
        if not tstr:
            continue
        tclean = tstr.split(" ")[0]
        hour, minute = [int(x) for x in tclean.split(":")]
        naive = datetime(year, month, day, hour, minute)
        aware = timezone.localize(naive)
        prayer_dt[name] = aware.astimezone(pytz.utc)
    return prayer_dt, tz_name

class PrayerScheduler:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.session = aiohttp.ClientSession()
        self.current_day = None
        self.prayer_times = {}  # name -> UTC datetime
        self.api_tz = "Europe/London"
        self.task = bot.loop.create_task(self.runner())

    async def refresh_for_today(self):
        today = date.today()
        if self.current_day == today:
            return
        api_data = await fetch_prayer_times(self.session, today)
        prayer_dt, tz_name = build_prayer_datetimes(api_data)
        self.prayer_times = prayer_dt
        self.api_tz = tz_name
        self.current_day = today
        print(f"[Scheduler] Loaded prayer times for {today} tz={tz_name}: {prayer_dt}")

    async def runner(self):
        await self.bot.wait_until_ready()
        PING_OFFSET = timedelta(minutes=10)
        while not self.bot.is_closed():
            try:
                await self.refresh_for_today()
                now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)

                # Ping times shifted 10 minutes earlier
                upcoming = [(name, dt - PING_OFFSET) for name, dt in self.prayer_times.items() if (dt - PING_OFFSET) > now_utc]
                if not upcoming:
                    tomorrow = datetime.utcnow().replace(tzinfo=pytz.utc) + timedelta(days=1)
                    next_midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 1, 0, tzinfo=pytz.utc)
                    sleep_seconds = (next_midnight - now_utc).total_seconds()
                    await asyncio.sleep(max(60, sleep_seconds))
                    continue

                upcoming.sort(key=lambda x: x[1])
                name, next_dt = upcoming[0]
                wait = (next_dt - now_utc).total_seconds()
                if wait > 0:
                    await asyncio.sleep(min(wait, 3600))
                    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
                    if abs((next_dt - now_utc).total_seconds()) <= 65:
                        await self.send_prayer_ping(name)
                else:
                    await asyncio.sleep(1)
            except Exception as e:
                print("Scheduler error:", e)
                await asyncio.sleep(30)

    async def send_prayer_ping(self, name):
        for guild_id_str, info in guilds.items():
            try:
                if not info.get("enabled", False):
                    continue
                channel_id = info.get("channel_id")
                if not channel_id:
                    continue
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue
                original_time = self.prayer_times.get(name)
                local_time_str = original_time.astimezone(pytz.timezone(self.api_tz)).strftime("%H:%M %Z") if original_time else "Unknown"
                msg = f"@here **{name}** — Salah time in London at **{local_time_str}** is in 10 minutes!"
                await channel.send(msg)
            except Exception as e:
                print(f"Failed to send to guild {guild_id_str}: {e}")

scheduler = None

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Set the channel for London salah pings")
    @app_commands.describe(channel="The text channel to send prayer pings to")
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
            return
        guild_id = str(interaction.guild_id)
        guilds.setdefault(guild_id, {})
        guilds[guild_id]["channel_id"] = channel.id
        guilds[guild_id].setdefault("enabled", True)
        save_guilds(guilds)
        await interaction.response.send_message(f"Prayer pings set to {channel.mention} and enabled.", ephemeral=True)

    @app_commands.command(name="toggle", description="Toggle prayer pings on/off for this server")
    async def toggle(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
            return
        guild_id = str(interaction.guild_id)
        guilds.setdefault(guild_id, {})
        cur = guilds[guild_id].get("enabled", False)
        guilds[guild_id]["enabled"] = not cur
        save_guilds(guilds)
        await interaction.response.send_message(f"Prayer pings {'enabled' if not cur else 'disabled'}.", ephemeral=True)

    @app_commands.command(name="test", description="Show today’s London salah times")
    async def test(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission.", ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            api_data = await fetch_prayer_times(session, date.today())

        prayer_dt, tz_name = build_prayer_datetimes(api_data)
        timezone = pytz.timezone(tz_name)

        embed = discord.Embed(
            title=f"Prayer Times for London on {date.today().strftime('%d %b %Y')}",
            color=discord.Color.blue()
        )
        for name in ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]:
            dt = prayer_dt.get(name)
            if dt:
                local_time = dt.astimezone(timezone).strftime("%H:%M %Z")
                embed.add_field(name=name, value=local_time, inline=True)

        embed.set_footer(text="Times from AlAdhan API")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    global scheduler
    print(f"Bot ready as {bot.user} (ID {bot.user.id})")
    try:
        bot.tree.add_command(Setup(bot).setup)
        bot.tree.add_command(Setup(bot).toggle)
        bot.tree.add_command(Setup(bot).test)
    except Exception:
        pass

    try:
        await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Failed to sync app commands:", e)

    if scheduler is None:
        scheduler = PrayerScheduler(bot)

if __name__ == "__main__":
    keep_alive()  # start keep-alive webserver for UptimeRobot pings 
    from keep_alive import keep_alive
    
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        print("Set DISCORD_TOKEN env var.")
        exit(1)
    bot.run(TOKEN)