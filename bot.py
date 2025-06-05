import os
import subprocess
import discord
import json
import shlex
import asyncio
import sqlite3
import re
import datetime
import requests

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# TODO: Improve error handling and logging
# TODO: Add a check to ensure that download_failed does not get marked as True if it is trying to download next_episode before the air time
# TODO: Pull any failed episode downloads and retry if it makes sense to

# Load token from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
JELLYFIN_URL = os.getenv("JELLYFIN_URL")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

def load_config(path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found at {path}")
        with open(path, "r") as file:
            return json.load(file)

CONFIG_PATH = "config.json"
CONFIG = load_config(CONFIG_PATH)

async def command_allowed(interaction: discord.Interaction):
    allowed_servers = CONFIG.get("allowedServers", None)
    if allowed_servers and interaction.guild.id not in allowed_servers:
        await interaction.response.send_message(
            "This bot is disabled in this server.",
            ephemeral=True
        )
        return False
    return True

async def has_account(interaction: discord.Interaction):
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jellyfin_users (
            discord_id INTEGER PRIMARY KEY,
            jellyfin_username TEXT NOT NULL,
            jellyfin_password TEXT NOT NULL
        )
    ''')
    conn.commit()
    cursor.execute('''
        SELECT jellyfin_username FROM jellyfin_users WHERE discord_id = ?
    ''', (interaction.user.id))
    existing_user = cursor.fetchone()
    
    if existing_user:
        return True
    else:
        return False

@bot.tree.command(name="create_user", description="Create a user for the Jellyfin server")
@app_commands.describe(
    username="Username for the Jellyfin user (default: your Discord username)",
    password="Password for the Jellyfin user (default: a random password will be generated)"
)
async def create_user(interaction: discord.Interaction, username: str = None, password: str = None):    
    if not await command_allowed(interaction):
        return

    # Check if the user already exists
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jellyfin_users (
            discord_id INTEGER PRIMARY KEY,
            jellyfin_username TEXT NOT NULL,
            jellyfin_password TEXT NOT NULL
        )
    ''')
    conn.commit()
    cursor.execute('SELECT jellyfin_username, jellyfin_password FROM jellyfin_users WHERE discord_id = ?', (interaction.user.id,))
    existing_user = cursor.fetchone()
    
    if existing_user:
        existing_username, existing_password = existing_user
        await interaction.response.send_message(
            f"You already have a Jellyfin user linked:\nUsername: {existing_username}\nPassword: {existing_password}\n",
            ephemeral=True # Very important
        )
        conn.close()
        return True
    
    # Create a new Jellyfin user
    jellyfin_username = re.sub(r'\W+', '', username or interaction.user.name)
    jellyfin_password = password or os.urandom(4).hex()  # Generate a random password if not provided

    try:
        # Call the Jellyfin API to create a new user
        headers = {
            "X-Emby-Authorization": f'MediaBrowser Client="AutoBot", Device="DiscordBot", DeviceId="{interaction.user.id}", Version="1.0.0", Token="{JELLYFIN_API_KEY}"',
            "Content-Type": "application/json"
        }
        payload = {
            "Name": jellyfin_username,
            "Password": jellyfin_password,
            "IsAdministrator": False,  # Set to True if you want the user to have admin rights
            "EnableUserPreferenceAccess": True,
            "EnablePublicSharing": False,
            "EnableSyncTranscoding": True
        }
        response = requests.post(f"http://localhost:8096/Users/New", headers=headers, json=payload)
        if response.status_code in {200, 204}:
            # User created successfully, store in database
            cursor.execute('''
                INSERT INTO jellyfin_users (discord_id, jellyfin_username, jellyfin_password)
                VALUES (?, ?, ?)
            ''', (interaction.user.id, jellyfin_username, jellyfin_password))
            conn.commit()
            await interaction.response.send_message(
                f"Jellyfin user created successfully:\nUsername: {jellyfin_username}\nPassword: {jellyfin_password}\n",
                ephemeral=True
            )
            print(f"[+] Created Jellyfin user for {interaction.user.name} ({interaction.user.id})")
            conn.close()
            return True
        else:
            await interaction.response.send_message(
                f"[X] Failed to create Jellyfin user: {response.text}",
                ephemeral=True
            )
            print(f"[!] Failed to create Jellyfin user for {interaction.user.name} ({interaction.user.id}): {response.text}")
    except Exception as e:
        await interaction.response.send_message(
            f"[X] An error occurred while creating the Jellyfin user: {str(e)}",
            ephemeral=True
        )
        print(f"[!] Exception during Jellyfin user creation for {interaction.user.name} ({interaction.user.id}): {e}")
    finally:
        conn.close()
        return False
             
async def create_tables(conn, cursor=None):
    if not cursor:
        cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS series (
            miruro_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode_count INTEGER,
            episodes_aired INTEGER,
            next_episode_time TIMESTAMP,
            next_episode INTEGER,
            last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_airing BOOLEAN DEFAULT 0,
            download_failed BOOLEAN DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            user_id TEXT NOT NULL,
            miruro_id TEXT NOT NULL,
            notify BOOLEAN DEFAULT 0,
            PRIMARY KEY (user_id, miruro_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS episodes (
            miruro_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            title TEXT,
            downloaded BOOLEAN DEFAULT 0,
            PRIMARY KEY (miruro_id, season, episode)
        )
    ''')
    conn.commit()

async def add_follow(msg, user_id, series_id, notify=False, dub=False, download_all=True):
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()
    await create_tables(conn, cursor)
    cursor.execute('''
        INSERT OR REPLACE INTO follows (user_id, miruro_id, notify)
        VALUES (?, ?, ?)
    ''', (user_id, series_id, notify))
    conn.commit()

    # Gather series info if not already done
    cursor.execute('SELECT * FROM series WHERE miruro_id = ?', (series_id,))
    series_info = cursor.fetchone()
    if not series_info:
        try:
            link = f"https://www.miruro.to/watch?id={series_id}&ep=1"
            full_cmd = ["python", "download.py", link, "--follow"] # No dub support but who cares
            print(f"Running command: {' '.join(full_cmd)}")  # Debugging line
            result = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            output = stdout.decode().strip() or "No output."
            error = stderr.decode().strip()

            response = await parse_download_response(result, output, error)

            if response == "<:eyebrowraised:1379311747277787207>":
                await msg.edit(content=response[:2000])  # Discord message limit
                conn.close()
                return "no"
        except Exception as e:
            print(f"[!] Error gathering info for series ID {series_id}: {e}")
            conn.close()
            return False

    if download_all:
        # Download the entire season (up to MAX_EPISODES)
        cursor.execute('''
            SELECT miruro_id, title, season, episode_count, episodes_aired, next_episode_time, next_episode, is_airing
            FROM series WHERE miruro_id = ?
        ''', (series_id,))
        series_info = cursor.fetchone()
        if not series_info:
            print(f"[!] Series ID {series_id} not found in database after info gathering.")
            conn.close()
            return False
        miruro_id, title, season, episode_count, episodes_aired, next_episode_time, next_episode, is_airing = series_info
        print(f"[+] Attempting to download all of {title} Season {season} (ID: {miruro_id})")
        if episode_count is None:
            print(f"[!] Episode count for series ID {series_id} is not set. Cannot proceed with download.")
            conn.close()
            return False
        
        episode_range = f"1-{episodes_aired + 1}" if episodes_aired < episode_count else f"1-{episode_count}"

        if episode_count > CONFIG.get("MAX_EPISODES", 25):
            episode_range = f"{(episodes_aired + 1) - CONFIG.get('MAX_EPISODES', 30) + 1}-{episodes_aired + 1}"
            print(f"[!] Episode count for series '{title} Season {season}' exceeds maximum episode count. "
                f"Downloading only {CONFIG.get('MAX_EPISODES', 30)} most recent episodes. ")
            
        args = f"--episodes {episode_range}"
        if dub:
            args += " --dub"
        try:
            full_cmd = ["python", "download.py", f"https://www.miruro.to/watch?id={series_id}&ep=1"] + shlex.split(args)
            print(f"Running command: {' '.join(full_cmd)}")  # Debugging line
            result = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            if result.returncode != 0:
                print(f"[!] Download failed for series ID {series_id}: {stderr.decode().strip()}")
                print(f"[!] Output: {stdout.decode().strip()}")
                conn.close()
                return False
            print(f"[+] Download completed for series ID {series_id}: {stdout.decode().strip()}")
            print(f"[+] Output: {stdout.decode().strip()}")
        except Exception as e:
            print(f"[!] Exception during download for series ID {series_id}: {e}")
            conn.close()
            return False


    conn.close()
    return True

async def parse_download_response(result, output, error):
    if result.returncode != 0:
        # 1 indicates invalid episode number, 2 indicates download error, 3 cancelled server side
        if result.returncode == 1:
            error_msg = "[X] Invalid episode number specified."
        elif result.returncode == 2:
            error_msg = "[X] Download error occurred. Please check the link and try again."
        elif result.returncode == 3:
            error_msg = "[X] Download cancelled by the server."
        elif result.returncode == 69:
            error_msg = "<:eyebrowraised:1379311747277787207>"
        else:
            error_msg = f"[X] Script exited with code {result.returncode}."
        response = f"{error_msg}"
        #response = f"{error_msg}\n```{error or output}```"
    else:
        response = f"[+] Download(s) completed successfully."
        # response = f"Download(s) completed successfully.\n```{output}```"
    return response

@bot.tree.command(name="link", description="Get the direct link to the Jellyfin server")
async def link(interaction: discord.Interaction):
    if not await command_allowed(interaction):
        return

    await interaction.response.defer()  # Defer the response

    # Tell the user to create an account
    account_message = ""
    if has_account(interaction):
        account_message = f"\nTo access it, create an account with the /create_user command"

    await interaction.followup.send(
        f"Direct link to the Jellyfin server: {JELLYFIN_URL}{account_message}\nDownload episodes using links from https://www.miruro.to/",
    )

@bot.tree.command(name="follow", description="Follow a series to automatically download new episodes")
@app_commands.describe(
    link="Direct link to the series page",
    notify="Whether to enable notifications for new episodes (default: true)",
    dub="Whether to follow the dubbed version of the series (default: false)"
)
async def follow(interaction: discord.Interaction, link: str, notify: bool = False, dub: bool = False):
    if not await command_allowed(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    # Block command until user has an account
    if not has_account(interaction):
        interaction.followup.send(
            "First, you need to create an account with the /create_user command."
        )
        return

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

    # Tell user that it is attempting to download all episodes
    msg = await interaction.followup.send(
        f"Attempting to follow series ID {SERIES_ID} and download all episodes. "
        "This may take a while depending on the number of episodes.",
        ephemeral=True
    )

    # Add or update the follow entry in the database
    followed = await add_follow(msg, interaction.user.id, SERIES_ID, notify, dub)
    if not followed :
        await msg.edit(content="[X] Failed to gather series information. Please check the link and try again.")
        return
    
    if followed == "no":
        return

    # Tell user that the series was successfully followed
    await msg.edit(content=f"Successfully followed and downloaded series ID {SERIES_ID}. ")

    await interaction.followup.send(
        f"Successfully followed series ID {SERIES_ID}.",
        ephemeral=True
    )

@bot.tree.command(name="notify", description="Get notified when new episodes air for a series")
@app_commands.describe(
    link="Direct link to the series page",
    notify="Whether to enable notifications for new episodes (default: true)",
    dub="Whether to follow the dubbed version of the series (default: false)"
)
async def notify(interaction: discord.Interaction, link: str, notify: bool = True, dub: bool = False):
    if not await command_allowed(interaction):
        return
    
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Block command until user has an account
    if not has_account(interaction):
        interaction.followup.send(
            "First, you need to create an account with the /create_user command."
        )
        return

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

    # Tell user that it is attempting to download all episodes
    msg = await interaction.followup.send(
        f"{'Enabl' if notify else 'Disabl'}ing notifications for id:{SERIES_ID}.",
        ephemeral=True
    )

    # Add or update the follow entry in the database
    following = await add_follow(msg, interaction.user.id, SERIES_ID, notify, dub)
    if not following:
        await msg.edit(content="[X] Failed to gather series information. Please check the link and try again.")
        return
    
    if following == "no":
        return
    

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
    if not await command_allowed(interaction):
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

    # Block command until user has an account
    if not has_account(interaction):
        interaction.followup.send(
            "First, you need to create an account with the /create_user command."
        )
        return

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
        # args += " --follow"

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
        add_follow(interaction.user.id, SERIES_ID, notify=False, dub=dub, download_all=False)

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
        f"Added download to queue: estimated minimum time is ~{estimated_time}",
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
        
        response = await parse_download_response(result, output, error)

        print(f"[>] Download result: {response}")
        await msg.edit(content=response[:2000])  # Discord message limit
    except Exception as e:
        print(f"[!] Exception during download: {e}")
        await msg.edit(content=f"[X] An unexpected error occurred:\n```{str(e)}```")

async def schedule_episode_checks():
    while True:
        try:
            await check_for_episodes()
            print("[*] Scheduler check complete.\n")
        except Exception as e:
            print(f"[!] Scheduler error: {e}")
        await asyncio.sleep((CONFIG.get('scanInterval', 10) * 60))  # wait however many minutes set in config

async def notify_users(miruro_id, title, new_episode, conn, cursor):
    cursor.execute('''
        SELECT user_id FROM follows WHERE miruro_id = ? AND notify = 1
    ''', (miruro_id,))
    users = cursor.fetchall()
    if not users:
        print(f"[*] No users to notify for series ID {miruro_id}.")
        return
    
    for user in users:
        user_id = user[0]
        try:
            user_obj = await bot.fetch_user(user_id)
            if user_obj:
                await user_obj.send(
                    f"New episode available for **{title}**: Episode {new_episode} is now available! "
                    f"\nCheck it out at {JELLYFIN_URL} or on the Swiftfin/Jellyfin app."
                )
                print(f"[+] Notified {user_obj.name} about new episode of {title}.")
        except discord.Forbidden:
            print(f"[!] Could not notify {user_id}: User has DMs disabled.")
        except Exception as e:
            print(f"[!] Error notifying {user_id}: {e}")

    return

async def check_for_episodes():
    conn = sqlite3.connect("hue.db")
    cursor = conn.cursor()

    print("[*] Checking for scheduled downloads...")

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Select series that are airing and have at least one follower
    cursor.execute('''
        SELECT DISTINCT s.miruro_id, s.next_episode, s.title, s.season, s.next_episode_time
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

    for miruro_id, next_episode, title, season, next_episode_time in series_to_download:
        try:
            # Check if the episode actually needs to be downloaded
            cursor.execute('''
                SELECT title FROM episodes WHERE miruro_id = ? AND season = ? AND episode = ?          
            ''', (miruro_id, season, next_episode))
            recent_episode = cursor.fetchone()

            if recent_episode:
                cursor.execute('''
                    SELECT title FROM episodes WHERE miruro_id = ? AND season = ? AND episode = ?
                ''', (miruro_id, season, 1))
                first_episode = cursor.fetchone()

                if first_episode and first_episode == recent_episode and datetime.datetime.fromisoformat(next_episode_time) > datetime.datetime.now(): # (First episode has been downloaded) and (newly aired episode wasnt available when it failed) and (new episode shouldnt have aired yet)
                    print(f"[!] Skipping download for {title} S{season}E{next_episode}. Episode name in DB matches episode 1")
                    continue

            print(f"[>] Attempting download for '{title} ep {next_episode}' (ID: {miruro_id})...")

            # Add dub flag if needed
            command = ["python", "download.py", f"https://www.miruro.to/watch?id={miruro_id}&ep={next_episode}"]
            if "(Dubbed)" in title:
                command.append("--dub")

            result = await asyncio.create_subprocess_exec(
                *command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            output = stdout.decode().strip() or "No output."
            error = stderr.decode().strip()

            if result.returncode == 0:
                print(f"[OK] Download successful for '{title}'")
                cursor.execute('''
                    UPDATE series SET download_failed = 0, last_checked = CURRENT_TIMESTAMP
                    WHERE miruro_id = ?
                ''', (miruro_id,))

                # Notify users who follow this series
                await notify_users(miruro_id, title, next_episode, conn, cursor)
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

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    # Start the scheduler in the background
    bot.loop.create_task(schedule_episode_checks())

bot.run(TOKEN)
