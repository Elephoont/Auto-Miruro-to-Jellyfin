import os
import subprocess
import discord
import json
import shlex
import asyncio
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# TODO: Improve error handling and logging
# TODO: Add a command to follow a show as it releases
# TODO: Make the bot responses only visible to the user who invoked the command

# Load token from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

CONFIG = {}
CONFIG_PATH = "config.json"

def load_config(path=CONFIG_PATH):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found at {path}")
        with open(path, "r") as file:
            return json.load(file)
        
@bot.tree.command(name="download", description="Download an episode from Miruro.to")
@app_commands.describe(
    link="Direct link to the episode page",
    episodes=f"Episode number or range to download (e.g. 1 or 2-5) MAX {CONFIG.get('MAX_EPISODES', 25)} episodes",
    dub="Whether to download the dubbed version (default: false)"
)

async def download(interaction: discord.Interaction, link: str, episodes: str, dub: bool = False):
    await interaction.response.defer(thinking=True)

    # Determine whether a single episode or a range is specified
    if '-' in episodes:
        args = f"--episodes {episodes}"
        num_episodes = int(episodes.split('-')[1]) - int(episodes.split('-')[0]) + 1
    else:
        args = f"--episode {episodes}"
        num_episodes = 1
    if dub:
        args += " --dub"

    # Tell the user that the download is starting
    msg = await interaction.followup.send(
        f"Starting download: estimated time is ~{30 * num_episodes} seconds...",
        wait=True
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

@bot.event
async def on_ready():
    # Load configuration
    global CONFIG
    CONFIG = load_config()
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

bot.run(TOKEN)
