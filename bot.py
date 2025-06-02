import os
import subprocess
import discord
import json
import shlex
import asyncio
import sqlite3
import re
import datetime
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# TODO: Improve error handling and logging
# TODO: Add a command to follow a show as it releases (or a flag)
# TODO: Fix time estimate formatting

# Load token from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
JELLYFIN_URL = os.getenv("JELLYFIN_URL")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def load_config(path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found at {path}")
        with open(path, "r") as file:
            return json.load(file)

CONFIG_PATH = "config.json"
CONFIG = load_config(CONFIG_PATH)
        
async def add_follow(user_id, series_id, notify=False):
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            user_id TEXT NOT NULL,
            miruro_id TEXT NOT NULL,
            notify BOOLEAN DEFAULT 0,
            PRIMARY KEY (user_id, miruro_id)
        )
    ''')
    cursor.execute('''
        INSERT OR REPLACE INTO follows (user_id, miruro_id, notify)
        VALUES (?, ?, ?)
    ''', (user_id, series_id, notify))
    conn.commit()
    conn.close()

    # Download the first episode if not already downloaded
    link = f"https://www.miruro.to/watch?id={series_id}&ep=1"
    full_cmd = ["python", "download.py", link] # No dub support but who cares
    print(f"Running command: {' '.join(full_cmd)}")  # Debugging line
    result = await asyncio.create_subprocess_exec(
        *full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    stdout, stderr = await result.communicate()

@bot.tree.command(name="link", description="Get the direct link to the Jellyfin server")
async def link(interaction: discord.Interaction):
    await interaction.response.defer()  # Defer the response
    await interaction.followup.send(
        f"Direct link to the Jellyfin server: {JELLYFIN_URL}",
    )

@bot.tree.command(name="follow", description="Follow a series to automatically download new episodes")
@app_commands.describe(
    link="Direct link to the series page",
    notify="Whether to enable notifications for new episodes (default: true)"
)
async def follow(interaction: discord.Interaction, link: str, notify: bool = False):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Validate the link format (valid tld are .to, .tv, .online)
    if not re.match(r'^https?://(www\.)?miruro\.(to|tv|online)/watch\?id=\d+(&ep=\d+)?$', link):
        await interaction.followup.send(
            "[X] Invalid link format. Please provide a valid Miruro series link.",
            ephemeral=True
        )
        return

    # Extract series ID from the link
    SERIES_ID = re.search(r'id=(\d+)', link)
    if SERIES_ID:
        SERIES_ID = SERIES_ID.group(1)
        print(f"[*] Series ID: {SERIES_ID}")
    else:
        await interaction.followup.send(
            "[X] Could not determine series ID from URL. "
            "Please ensure the URL is correct and contains a valid series ID.",
            ephemeral=True
        )
        return

    # Add or update the follow entry in the database
    await add_follow(interaction.user.id, SERIES_ID, notify)

    await interaction.followup.send(
        f"Successfully followed series ID {SERIES_ID}.",
        ephemeral=True
    )

@bot.tree.command(name="notify", description="Get notified when new episodes air for a series")
@app_commands.describe(
    link="Direct link to the series page",
    notify="Whether to enable notifications for new episodes (default: true)"
)
async def notify(interaction: discord.Interaction, link: str, notify: bool = True):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Validate the link format (valid tld are .to, .tv, .online)
    if not re.match(r'^https?://(www\.)?miruro\.(to|tv|online)/watch\?id=\d+(&ep=\d+)?$', link):
        await interaction.followup.send(
            "[X] Invalid link format. Please provide a valid Miruro series link.",
            ephemeral=True
        )
        return

    # Extract series ID from the link
    SERIES_ID = re.search(r'id=(\d+)', link)
    if SERIES_ID:
        SERIES_ID = SERIES_ID.group(1)
        print(f"[*] Series ID: {SERIES_ID}")
    else:
        await interaction.followup.send(
            "[X] Could not determine series ID from URL. "
            "Please ensure the URL is correct and contains a valid series ID.",
            ephemeral=True
        )
        return

    # Add or update the follow entry in the database
    await add_follow(interaction.user.id, SERIES_ID, notify)

    await interaction.followup.send(
        f"Successfully {'enabled' if notify else 'disabled'} notifications for series ID {SERIES_ID}.",
        ephemeral=True
    )

@bot.tree.command(name="download", description="Download an episode from Miruro.to")
@app_commands.describe(
    link="Direct link to the episode page",
    episodes=f"Episode number or range to download (e.g. 1 or 2-5) [MAX {CONFIG.get('MAX_EPISODES', 25)} episodes]",
    dub="Whether to download the dubbed version (default: false)",
    follow="Automatically download new episodes as they release (default: false)"
)
async def download(interaction: discord.Interaction, link: str, episodes: str = "0", dub: bool = False, follow: bool = False):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Validate the link format (valid tld are .to, .tv, .online)
    if not re.match(r'^https?://(www\.)?miruro\.(to|tv|online)/watch\?id=\d+(&ep=\d+)?$', link):
        await interaction.followup.send(
            "[X] Invalid link format. Please provide a valid Miruro series link.",
            ephemeral=True
        )
        return

    # Determine whether a single episode or a range is specified
    if '-' in episodes:
        args = f"--episodes {episodes}"
        if not episodes.split('-')[0].isdigit() or not episodes.split('-')[1].isdigit():
            await interaction.followup.send(
                "[X] Invalid episode range specified. Please use a valid range like 1-5.",
                ephemeral=True
            )
            return
        if int(episodes.split('-')[1]) < int(episodes.split('-')[0]):
            await interaction.followup.send(
                "[X] Invalid episode range specified. The end of the range must be greater than or equal to the start.",
                ephemeral=True
            )
            return
        num_episodes = int(episodes.split('-')[1]) - int(episodes.split('-')[0]) + 1
    else:
        args = f"--episode {episodes}"
        num_episodes = 1
    if dub:
        args += " --dub"
    
   

    if follow:
        args += " --follow"

        SERIES_ID = re.search(r'id=(\d+)', link)
        if SERIES_ID:
            SERIES_ID = SERIES_ID.group(1)
            print(f"[*] Series ID: {SERIES_ID}")
        else:
            await interaction.followup.send(
                "[X] Could not determine series ID from URL. "
                "Please ensure the URL is correct and contains a valid series ID.",
                ephemeral=True
            )
            return

        # Add or update the follow entry in the database
        add_follow(interaction.user.id, SERIES_ID, notify=False)

    if num_episodes > CONFIG.get("MAX_EPISODES", 25):
        await interaction.followup.send(
            f"[X] You can only download up to {CONFIG['MAX_EPISODES']} episodes at a time.",
            ephemeral=True
        )
        return
    
    # Convert the estimated time to minutes with decimal places
    estimated_time = num_episodes * 30
    if estimated_time > 60:
        estimated_time = f"{estimated_time / 60:.1f} minutes"
    else:
        estimated_time = f"{estimated_time} seconds"
    
    # # Tell the user that the download is starting
    msg = await interaction.followup.send(
        f"Starting download: estimated time is ~{estimated_time}",
        wait=True,
        ephemeral=True
    )

    try:
        # Run download script and capture output/errors
        full_cmd = ["python", "download.py", link] + shlex.split(args)
        print(f"Running command: {' '.join(full_cmd)}")  # Debugging line
        result = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        output = stdout.decode().strip() or "No output."
        error = stderr.decode().strip()
        
        if result.returncode != 0:
            # 1 indicates invalid episode number, 2 indicates download error, 3 cancelled server side
            if result.returncode == 1:
                error_msg = "[X] Invalid episode number specified."
            elif result.returncode == 2:
                error_msg = "[X] Download error occurred. Please check the link and try again."
            elif result.returncode == 3:
                error_msg = "[X] Download cancelled by the server."
            else:
                error_msg = f"[X] Script exited with code {result.returncode}.\n```{error or output}```"
            response = f"{error_msg}\n```{error or output}```"
        else:
            response = f"Script completed.\n```{output}```"

        await msg.edit(content=response[:2000])  # Discord message limit
    except Exception as e:
        await msg.edit(content=f"[X] An unexpected error occurred:\n```{str(e)}```")

async def schedule_episode_checks():
    while True:
        try:
            await check_for_episodes()
        except Exception as e:
            print(f"[!] Scheduler error: {e}")
        await asyncio.sleep((CONFIG.get('scanInterval', 10) * 60))  # wait however many minutes set in config

async def check_for_episodes():
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()

    print("[*] Checking for scheduled downloads...")

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Select series that are airing and have at least one follower
    cursor.execute('''
        SELECT DISTINCT s.miruro_id, s.next_episode, s.title
        FROM series s
        JOIN follows f ON s.miruro_id = f.miruro_id
        WHERE s.is_airing = 1
          AND (
              s.download_failed = 1 OR
              (s.next_episode_time IS NOT NULL AND ? >= datetime(s.next_episode_time))
          )
    ''', (now,))

    series_to_download = cursor.fetchall()
    if not series_to_download:
        print("[*] No new episodes to download.")
        conn.close()
        return
    
    for miruro_id, next_episode, title in series_to_download:
        try:
            print(f"[>] Attempting download for '{title}' (ID: {miruro_id})...")

            # Add dub flag if needed
            command = ["python", "download.py", f"https://www.miruro.to/watch?id={miruro_id}&ep={next_episode}"]
            if "(Dubbed)" in title:
                command.append("--dub")

            result = subprocess.run(
                command,
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                print(f"[OK] Download successful for '{title}'")
                cursor.execute('''
                    UPDATE series SET download_failed = 0, last_checked = CURRENT_TIMESTAMP
                    WHERE miruro_id = ?
                ''', (miruro_id,))
            else:
                print(f"[X] Download failed for '{title}'. Error:\n{result.stderr}")
                cursor.execute('''
                    UPDATE series SET download_failed = 1, last_checked = CURRENT_TIMESTAMP
                    WHERE miruro_id = ?
                ''', (miruro_id,))

        except Exception as e:
            print(f"[!] Exception during download for {miruro_id}: {e}")
            cursor.execute('''
                UPDATE series SET download_failed = 1, last_checked = CURRENT_TIMESTAMP
                WHERE miruro_id = ?
            ''', (miruro_id,))

    conn.commit()
    conn.close()
    print("[*] Scheduler check complete.\n")


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    # Start the scheduler in the background
    bot.loop.create_task(schedule_episode_checks())

bot.run(TOKEN)
