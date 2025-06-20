from playwright.sync_api import sync_playwright, TimeoutError
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
import time
import json
import argparse
import os
import re
import sys
import sqlite3
import portalocker
import xml.etree.ElementTree as ET
import warnings
import unicodedata

warnings.filterwarnings("ignore", message="The default datetime adapter is deprecated*", category=DeprecationWarning)

# TODO: Add support for other streaming servers with download links if they are added to the site
# TODO: Add a way to enable developer mode on first run by committing a seed chromium profile with only preferences
# TODO: Add support for shows that havent begun airing
# TODO: Add support for browser fingerprint spoofing
# TODO: Fix errors showing when download script incorrectly tries to download episode that has not aired
# TODO: Fix episodes getting overwritten for Part 2 of a season. (Either combine them into one episode or keep them separate)
# TODO: Notify user which episodes failed to download and which succeeded
# TODO: Find a way to ensure the 1080p version is downloaded
# TODO: Move away from using global variables. Create objects that are passed to each function
# TODO: Make it so that if the episode exists but not the nfo, it will attempt to create just the nfo file
# TODO: Add "airs before" and "airs after" tags for seasons and/or episodes
# TODO: Ignore Miruro completely and skip the pahe.win countdown

# TODO: Fix .nfo utf-8 encoding not supporting a middle dot

# Load jellyfin API key from .env file
load_dotenv()
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")

# Path to unpacked uBlock Origin extension
UBLOCK_PATH = os.path.abspath("./uBlock0.chromium")

# Output directory for downloaded video
OUTPUT_DIR = os.path.abspath("./output") # Default if not set in config
config = None
MAX_RETRIES = 3
MAX_EPISODES = 25  # Maximum episodes to download in one run
CONFIG_PATH = "config.json"
LOCK_FILE = "download.lock"

OUTPUT_NAME = "episode.mp4" X
SERIES_TITLE = "Unknown Series"
SERIES_ID = None
SEASON_NUMBER = 1
EPISODE_NUMBER = 0
EPISODE_NAME = "Unknown Episode"
FOLLOW = False  # Whether to follow the series and download new episodes as they release
conn = None
cursor = None
DUB = False  # Default to subbed unless specified
EPISODES_IN_SEASON = 0 # Number of episodes in the selected season for range validation
EPISODES_AIRED = 0
AIRING = False

class Config():
    def __init__(self, config_path):
        self.config_json = self.load_config(config_path)
        self.max_episodes = int(self.config_json.get("maxEpisodes", 30))
        self.output_dir = os.path.abspath(self.config_json.get("outputDir", "./output"))
        self.ban_NSFW = bool(self.config_json.get("banNSFW", True))
        self.retry_delay = int(self.config_json.get("retryDelay", 5))
        self.max_retries = int(self.config_json.get("maxRetries", 3))

    def load_config(path=CONFIG_PATH):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found at {path}")
        with open(path, "r") as file:
            return json.load(file)

class Show_Info():
    def __init__(self, miruro_url: str, follow: bool):
        self.miruro_url = miruro_url
        self.miruro_id = self.parse_miruro_url(self, miruro_url)
        self.follow = follow
        self.season_number = 1  # Default value
        self.episode_number = 0 # Default value
        self.formatted_season_number = f"{self.season_number:02}"
        self.formatted_episode_number = f"{self.episode_number:02}"
        self.episode_range = None
        self.episode_name = None
        self.series_name = None
        self.config = config
        self.mal_id = None
        self.pahe_id = None

        self.dub = False        # Default value
        self.episodes_in_season = None
        self.episodes_aired = None
        self.airing = False     # Default value

    def parse_miruro_url(self, miruro_url):
        series_id = re.search(r'id=(\d+)', miruro_url)
        if series_id:
            self.miruro_id = series_id.group(1)
            print(f"[*] Series ID: {self.miruro_id}")
        else:
            print("[!] Could not determine series ID from URL. Please ensure the URL is correct and contains a valid series ID.")
            sys.exit(1)

    def confirm_episodes(self, args):
        if args.episode:                                # If there is a single episode number
            self.episode_number = args.episode
            self.episode_range = (EPISODE_NUMBER, EPISODE_NUMBER)
            
        elif args.episodes and '-' in args.episodes:    # If there is a range of episodes to parse
            ep_range = args.episodes.split('-')
            if len(ep_range) != 2 or not all(x.isdigit() for x in ep_range):
                raise ValueError("Invalid episode range format. Use 'start-end' (e.g. 1-5).")   
            if ep_range[0] <= 0 or ep_range[1] <= 0:
                raise ValueError("Episode numbers must be positive integers.")
            if ep_range[0] > ep_range[1]:
                raise ValueError("Start episode must be less than or equal to end episode.")
            if ep_range[1] - ep_range[0] + 1 > config.max_episodes:
                raise ValueError(f"Cannot download more than {config.max_episodes} episodes at once.")
            self.episode_range = (int(ep_range[0]), int(ep_range[1]))
        else:
            # Ensure the URL contains &ep=NUM at the end
            if self.miruro_url.rsplit("&ep=", 1)[-1].isdigit():
                ep_number = int(re.sub("[^0-9]", "", args.url[-4:]))
                if ep_number <= 0:
                    raise ValueError("Could not determine episode number from URL. "
                                    "Please specify with --episode or --episodes.")
                self.episode_range = (ep_number, ep_number)
                self.episode_number = ep_number
            else:
                raise ValueError("No episode number found in URL. "
                                "Please specify with --episode or --episodes.")

class Episode_Info():
    def __init__(self, show_info: Show_Info, episode_number: int):
        self.show_info = show_info
        self.episode_number = episode_number
        self.episode_name = None
        self.pahewin_url= None
        self.kwiksi_url = None
        self.conn = None
        self.cursor = None
        self.downloaded = None
        self.output_name = None

    def episode_downloaded(self):
        # Before opening the browser, check if the episode has already been downloaded
        conn = sqlite3.connect("hue.db")
        cursor = conn.cursor()

        cursor.execute('''
            SELECT downloaded FROM episodes
            WHERE miruro_id = ? AND episode = ? AND downloaded = 1
        ''', (self.show_info.miruro_id, self.episode_number))
        row = cursor.fetchone()

        # If episode exists in the database and is indicated as downloaded, check if the file really exists
        if row and row[0]:
            cursor.execute('''
                SELECT title, season FROM series WHERE miruro_id = ?
            ''', (self.show_info.miruro_id,))
            series_row = cursor.fetchone()
            if series_row:
                self.show_info.series_name, self.show_info.season_number = series_row
            self.output_name = os.path.join(config.output_dir, self.show_info.series_name, f"Season {self.episode_number:02}", f"{self.show_info.series_name} S{self.show_info.season_number:02}E{self.episode_number:02}.mp4")

            print(f"[*] Checking if filename {self.output_name} exists...")
            if os.path.exists(self.output_name): # Need to query DB for series name etc.
                print(f"[!] Episode {self.episode_number} of ID:{self.show_info.miruro_id} is already downloaded.")
                # Check if .nfo exists for this episode and create one if it doesn't
                conn.close()
                return True
            print("[*] Database indicates episode is downloaded, but file does not exist. ")

            # Update DB to say the episode was not downloaded
            cursor.execute('''
                UPDATE episodes
                SET downloaded = 0
                WHERE miruro_id = ? AND episode = ?
            ''', (self.show_info.miruro_id, self.episode_number))
            conn.commit()
            return False
        conn.close()
        return False

def acquire_download_lock():
    lock_file = open(LOCK_FILE, "w")
    print("[*] Waiting to acquire download lock...")
    portalocker.lock(lock_file, portalocker.LOCK_EX)  # Will block here until lock is free
    print("[OK] Lock acquired.")
    return lock_file

def get_kwik_download_page_OLD(miruro_url):
    global SERIES_TITLE, SEASON_NUMBER
    # Before opening the browser, check if the episode has already been downloaded
    cursor.execute('''
        SELECT downloaded FROM episodes
        WHERE miruro_id = ? AND episode = ? AND downloaded = 1
    ''', (SERIES_ID, EPISODE_NUMBER))
    row = cursor.fetchone()

    # If episode exists in the database and is indicated as downloaded, check if the file really exists
    if row and row[0]:
        cursor.execute('''
            SELECT title, season FROM series WHERE miruro_id = ?
        ''', (SERIES_ID,))
        series_row = cursor.fetchone()
        if series_row:
            SERIES_TITLE, SEASON_NUMBER = series_row
        OUTPUT_NAME = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER:02}", f"{SERIES_TITLE} S{SEASON_NUMBER:02}E{EPISODE_NUMBER:02}.mp4")

        print(f"[*] Checking if filename {OUTPUT_NAME} exists...")
        if os.path.exists(OUTPUT_NAME): # Need to query DB for series name etc.
            print(f"[!] Episode {EPISODE_NUMBER} of ID:{SERIES_ID} is already downloaded.")
            # Check if .nfo exists for this episode and create one if it doesn't
            return "skip"
        print("[*] Database indicates episode is downloaded, but file does not exist. ")

        # Update DB to say the episode was not downloaded
        cursor.execute('''
            UPDATE episodes
            SET downloaded = 0
            WHERE miruro_id = ? AND episode = ?
        ''', (SERIES_ID, EPISODE_NUMBER))
        conn.commit()


    with sync_playwright() as p:
        user_data_dir = os.path.abspath("chromium_user_data")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=[
                f'--disable-extensions-except={UBLOCK_PATH}',
                f'--load-extension={UBLOCK_PATH}',
            ]
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        print("[*] Opening miruro.to page...")
        page.goto(miruro_url)
        page.wait_for_timeout(5000)  # wait for JavaScript content

        # Get episodes_aired before gather_episode_info()
        info_blocks = page.query_selector_all("div.t4mg1tz p")
        episodes = None
        for block in info_blocks:
            text = block.inner_text().strip()
            if "Episodes" in text:
                match = re.search(r'Episodes:\s*([\d,]+)(?:\s*/\s*(\d+))?', text) # optionally looks for total episodes
                if match:
                    episodes = match
                    break
        print(f"[+] {episodes} episodes found in the series info block.")
        if episodes:
            EPISODES_AIRED = int(episodes.group(1).replace(',', ''))
            EPISODES_IN_SEASON = int(episodes.group(2).replace(',', '')) if episodes.group(2) else EPISODES_AIRED + 2 # Default to aired + 2 if not specified

        # Check if the requested episode number matches the page
        match = re.search(r'&ep=(\d+)', miruro_url)
        if match:
            requested_episode = int(match.group(1))
        else:
            print("[X] Error: Could not determine episode number from URL. "
                    "Please specify with --episode or --episodes.")
            conn.close()
            sys.exit(1)
        if requested_episode not in range(1, EPISODES_AIRED + 2):  # +2 because the aired count is sometimes off by 1
            if requested_episode not in range(1, EPISODES_IN_SEASON + 1):
                print(f"[X] Error: Episode {requested_episode} is not valid for this season. "
                    f"Only episodes 1 to {EPISODES_IN_SEASON} are available in this season.")
                conn.close()
                sys.exit(1) # 1 for invalid episode number
            else:
                print(f"[X] Error: Episode {requested_episode} has not aired yet. "
                    f"Only episodes 1 to {EPISODES_AIRED + 1 if EPISODES_AIRED + 1 <= EPISODES_IN_SEASON else EPISODES_AIRED} have aired.")
                conn.close()
                sys.exit(1)

        # Check if the page is on the correct episode
        ep_number_element = page.query_selector(".title-container .ep-number")
        if not ep_number_element:
            raise ValueError("Could not find episode number on page.")
        match = re.search(r'\d+', ep_number_element.inner_text().strip())
        ep_number_element = int(match.group(0))
        if ep_number_element != requested_episode:
            raise ValueError(f"Current episode ({ep_number_element}) does not match requested episode ({requested_episode}). "
                             "Please check the URL or specify the episode with --episode or --episodes.")

        # Now that the page has been confirmed to be correct, gather basic info about the episode/series
        gather_episode_info(page, browser)

        # Create the appropriate directories if they dont exist
        os.makedirs(os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {int(SEASON_NUMBER):02}"), exist_ok=True)

        # Now write .nfo files to ensure jellyfin has reliable metadata
        parse_metadata(page, browser, SERIES_ID, SERIES_TITLE)

        if FOLLOW:
            print("[*] Following the series. No download will be performed.") # Bot script should next attempt to download the whole season
            browser.close()
            return "skip"

        # Check if the correct playback server is selected
        print("[*] Checking if playback server is Kiwi...")
        ensure_kiwi_server_selected(page)
        print("[OK] Kiwi server is selected under Sub section.")

        print("[*] Waiting for 'Download Episode' button...")
        try:
            page.wait_for_selector('button[title="Download Episode"]', timeout=15000)
            page.click('button[title="Download Episode"]')
            print("[+] Clicked the Download Episode button.")
        except TimeoutError:
            raise Exception("Could not find or click the Download Episode button")

        print("[*] Waiting for new tab to open...")
        new_page = browser.wait_for_event("page", timeout=15000)
        new_page.wait_for_load_state()
        print("[+] Switched to new tab (likely pahe.win).")

        print("[*] Polling for 'a.redirect' link...")
        for i in range(30):
            element = new_page.query_selector("a.redirect")
            if element:
                href = element.get_attribute("href")
                text = element.inner_text()
                print(f"[{i+1:02}s] Text: '{text}' | Href: {href}")
                if href and href.startswith("https://kwik.si/f/"):
                    print(f"[OK] Found kwik.si URL: {href}")
                    browser.close()
                    return href
            else:
                print(f"[{i+1:02}s] 'a.redirect' not yet found.")
            new_page.wait_for_timeout(1000)

        browser.close()
        raise Exception("Timed out waiting for redirect button.")

def get_kwik_download_page(episode: Episode_Info, config: Config):
    if episode.episode_downloaded():
        return "skip"
    
    anilist_json_link = f"https://www.miruro.to/api/info/anilist/{episode.show_info.miruro_id}"
    # MAL ID is found in key "idMal"

    # Fetch the anilist json from the link
    anilist_json = request_json(anilist_json_link)

    episode.show_info.mal_id = anilist_json.get("idMal", None)

    status = anilist_json.get("status", None)

    if status == "AIRING":
        episode.show_info.airing = True
    else:
        episode.show_info.airing = False

    mal_json_link = f"https://www.miruro.to/api/episodes?malId={episode.show_info.mal_id}&ongoing={str(episode.show_info.airing).lower()}"

    # Fetch the mal json from the link
    mal_json = request_json(mal_json_link)

    # Gather info about the episode/series
    gather_episode_info(anilist_json, mal_json)

    # Get Anime Pahe ID
    pahe_id, pahe_obj = next(iter(mal_json.get("ANIMEPAHE", "").items()))
    episode.show_info.pahe_id = int(pahe_id)

    # Can add fetchType=&category=sub to get sub or dub
    pahe_json_link = f"https://www.miruro.to/api/sources?episodeId={episode.show_info.pahe_id}%2Fep-{episode.episode_number}&provider=animepahe"
    
    # Fetch the mal json from the link 
    pahe_json = request_json(pahe_json_link)

    episode.pahewin_url = pahe_json.get("download", None)

    if not episode.pahewin_url: # If download link cannot be found
        return False
 
    create_nfo(anilist_json, mal_json)

    with sync_playwright() as p:
        user_data_dir = os.path.abspath("chromium_user_data")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=True
        )

        page = browser.pages[0] if browser.pages else browser.new_page()

        print("[*] Opening pahe.win page...")
        page.goto(episode.pahewin_url)
        page.wait_for_load_state("networkidle")  # wait for page to load

        # Grab full page HTML to find the link in the javascript
        html_content = page.content()
        
        # Now that the HTML has been saved, the browser can be closed
        browser.close()

        # Find the kwik.si link within the js of the page before it is assigned to the button
        kwik_match = re.search(r'\.attr\("href","(https://kwik\.si/f/[^"]+)"\)', html_content)

        if kwik_match:
            episode.kwiksi_url = kwik_match.group(1)
            print(f"[+] Found kwik.si link: {episode.kwiksi_url}")
            return True
        else:
            print("[!] kwik.si link not found")
            return False
    
def request_json(link):
    # Fetch the json from the link and return it
    try:
        json = requests.get(link)

        if json.status_code != 200:
            print(f"[!] Failed to fetch json from {link}: {json.status_code}")
            return False
    except Exception as e:
        print(f"[X] Error fetching json from {link}: {e}")

    return json.json()

def gather_episode_info(anilist_json, mal_json):
    # Get the following data and save it to the DB:
    # Series Title, Episode Title, Season number, Episodes in the season, Episodes aired
    # Skip if tags include ECCHI or HENTAI


    return

def gather_episode_info_OLD(page, browser):
    global SERIES_TITLE, EPISODE_NAME, SEASON_NUMBER, EPISODE_NUMBER, OUTPUT_NAME, EPISODES_IN_SEASON, EPISODES_AIRED, AIRING
    SERIES_TITLE = page.query_selector("div.title.anime-title a").inner_text()
    if DUB:
        SERIES_TITLE += " (Dubbed)"

    if config.get("banNSFW", True):
        tags_div = page.query_selector("div.t4mg1tz > div[style*='flex-wrap']")
        blacklist = {"ECCHI", "HENTAI"}
        whitelist_titles = {
            "nogamenolife",
            "konosuba", 
            "mushokutensei", 
            "killlakill", 
            "mydress-updarling", 
            "weneverlearn"
        }
        normalized_title = SERIES_TITLE.lower().replace(" ", "")
        is_whitelisted = any(kw in normalized_title for kw in whitelist_titles)

        if tags_div and not is_whitelisted:
            tag_elements = tags_div.query_selector_all("a")
            tags = {tag.inner_text().strip().upper() for tag in tag_elements}
            print(f"[*] Tags found: {tags}")

            if blacklist & tags:
                print("[X] Blacklisted tag detected. Skipping this series.")
                browser.close()
                conn.close()
                sys.exit(69)

    if not EPISODES_IN_SEASON:
        info_blocks = page.query_selector_all("div.t4mg1tz p")
        episodes = None
        for block in info_blocks:
            text = block.inner_text().strip()
            if "Episodes" in text:
                match = re.search(r'Episodes:\s*([\d,]+)(?:\s*/\s*(\d+))?', text) # optionally looks for total episodes
                if match:
                    episodes = match
                    break
        print(f"[+] {episodes} episodes found in the series info block.")
        if episodes:
            EPISODES_AIRED = int(episodes.group(1).replace(',', ''))
            EPISODES_IN_SEASON = int(episodes.group(2).replace(',', '')) if episodes.group(2) else EPISODES_AIRED + 2 # Default to aired + 2 if not specified

    EPISODE_NAME = page.query_selector(".title-container .ep-title").inner_text()

    match = re.match(r'^(.*?)(?:\s+(Season\s+\d+))?(?:\s+Part\s*\d+|\s+Cour\s*\d+)?$', SERIES_TITLE, re.IGNORECASE)
    if match:
        series = match.group(1).strip()
        season = match.group(2).strip().replace("Season ", "") if match.group(2) else None
        SERIES_TITLE = series
        SEASON_NUMBER = int(season) if season else 1

    SEASON_NUMBER = f"{SEASON_NUMBER:02}"
    EPISODE_NUMBER = f"{EPISODE_NUMBER:02}"
 
    # Remove characters not allowed in Windows directory names
    SERIES_TITLE = re.sub(r'[<>:"/\\|?*]', '', SERIES_TITLE)
    OUTPUT_NAME = os.path.join(SERIES_TITLE, f"Season {SEASON_NUMBER}", f"{SERIES_TITLE} S{SEASON_NUMBER}E{EPISODE_NUMBER}.mp4")

    print(f"[+] Series: {SERIES_TITLE} | Season: {SEASON_NUMBER} | Episode: {EPISODE_NAME}")

    # Determine if the series is currently airing
    AIRING = False  # Default

    status_elements = page.query_selector_all("div.t4mg1tz p")
    for element in status_elements:
        text = element.inner_text().strip().lower()
        if text.startswith("status:"):
            AIRING = "airing" in text
            break

    # find airing day and time
    NEXT_EPISODE_TIMESTAMP = None
    NEXT_EPISODE_NUMBER = None

    if AIRING:
        airing_div = page.query_selector("div.eb48q8z > p")
        if airing_div:
            airing_text = airing_div.inner_text().strip()
            print(f"[*] Found airing info: {airing_text}")

            # Try to match the date string from the text
            match = re.search(r'Episode (\d+)\s+will air on\s+(\w{3} \w{3} \d{1,2}), (\d{4}), (\d{2}:\d{2}) ([A-Z]+)', airing_text)
            if match:
                NEXT_EPISODE_NUMBER = int(match.group(1))
                date_str = f"{match.group(2)} {match.group(3)} {match.group(4)}"
                try:
                    NEXT_EPISODE_TIMESTAMP = datetime.strptime(date_str, "%a %b %d %Y %H:%M")
                    print(f"[*] Next episode number: {NEXT_EPISODE_NUMBER}")
                    print(f"[*] Parsed airing timestamp: {NEXT_EPISODE_TIMESTAMP}")
                except ValueError as e:
                    print(f"[!] Error parsing date: {e}")
            else:
                print("[!] Could not find next episode airing date.")
                # Fallback to current time + 1 day
                NEXT_EPISODE_TIMESTAMP = datetime.now() + timedelta(days=1)
        else:
            print("[!] Airing info div not found")
    else:
        print("[*] Series is not currently airing. Setting next episode to None.")
        EPISODES_IN_SEASON = EPISODES_AIRED  # If not airing, assume all episodes have aired
        NEXT_EPISODE_TIMESTAMP = None
        NEXT_EPISODE_NUMBER = None

    # Update the database to track episodes
    cursor.execute('''
            INSERT OR REPLACE INTO episodes (miruro_id, season, episode, title, downloaded)
            VALUES (?, ?, ?, ?, 0)
        ''', (SERIES_ID, int(SEASON_NUMBER), int(EPISODE_NUMBER), EPISODE_NAME))
    cursor.execute('''
        INSERT OR REPLACE INTO series (miruro_id, title, season, episode_count, episodes_aired, next_episode_time, next_episode, is_airing, last_checked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ''', (SERIES_ID, SERIES_TITLE, int(SEASON_NUMBER), EPISODES_IN_SEASON, EPISODES_AIRED, NEXT_EPISODE_TIMESTAMP, NEXT_EPISODE_NUMBER, AIRING))
    conn.commit()

def download_image(url, dest_path):
    if not url:
        print(f"[!] No URL provided for image: {dest_path}")
        return
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(response.content)
        print(f"[OK] Downloaded image to {dest_path}")
    except Exception as e:
        print(f"[X] Failed to download image {url}: {e}")

def parse_metadata(page, browser, miruro_id, SERIES_TITLE):
    # Find the href for the MAL link
    href = page.get_attribute("a[href^='https://myanimelist.net/anime/']", "href")

    # Extract the MAL ID from the href
    if href:
        mal_id = href.rstrip("/").split("/")[-1]
        print(f"[+] Found MAL ID: {mal_id}")
    else:
        print("[X] MAL ID not found.")

    anilist_json_link = f"https://www.miruro.to/api/info/anilist/{miruro_id}"
    mal_json_link = f"https://www.miruro.to/api/episodes?malId={mal_id}&ongoing={str(AIRING).lower()}"

    print(f"[*] Anilist JSON link: {anilist_json_link}")
    print(f"[*] MAL JSON link: {mal_json_link}")

    try:
        info_res = requests.get(anilist_json_link)
        episodes_res = requests.get(mal_json_link)

        if info_res.status_code != 200:
            print(f"[!] Failed to fetch AniList metadata: {info_res.status_code}")
            return False

        if episodes_res.status_code != 200:
            print(f"[!] Failed to fetch episode list: {episodes_res.status_code}")
            return False

        create_nfo(info_res.json(), episodes_res.json())

    except Exception as e:
        print(f"[X] Error fetching metadata: {e}")

def create_nfo(anilist_json, mal_json):
    write_series_nfo(anilist_json, mal_json, SERIES_TITLE)
    write_episode_nfo(anilist_json, mal_json, SERIES_TITLE)
    return

def safe_unicode(text):
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return unicodedata.normalize("NFC", text)

def write_series_nfo(anilist_json, mal_json, SERIES_TITLE): # info, episodes (respectively)
    if anilist_json.get("coverImage", {}).get("extraLarge"):
        poster = anilist_json.get("coverImage", {}).get("extraLarge", "")
    else:
        poster = anilist_json.get("coverImage", {}).get("large", "")

    TMDB_id, TMDB_obj = next(iter(mal_json.get("TMDB", "").items()))
    TMDB_id = int(TMDB_id)
    backdrop_url = TMDB_obj.get("metadata", {}).get("tvShowDetails", {}).get("show", {}).get("backdrop_path", "")
    backdrop_url = f"https://image.tmdb.org/t/p/original{backdrop_url}"

    if int(SEASON_NUMBER) > 1: # Specific season. Write nfo as season specific
        season = ET.Element("season")

        ET.SubElement(season, "title").text = safe_unicode(anilist_json.get("title", {}).get("english", ""))
        ET.SubElement(season, "seasonnumber").text = safe_unicode(str(int(SEASON_NUMBER)))
        ET.SubElement(season, "year").text = safe_unicode(str(anilist_json.get("startDate", {}).get("year", "")))
        ET.SubElement(season, "plot").text = safe_unicode(anilist_json.get("description", ""))
        ET.SubElement(season, "rating").text = safe_unicode(str(anilist_json.get("averageScore", "")))
        ET.SubElement(season, "thumb", {"aspect": "poster"}).text = safe_unicode(poster)
        # ET.SubElement(season, "thumb", {"aspect": "banner"}).text = safe_unicode(anilist_json.get("bannerImage", ""))
        tree = ET.ElementTree(season)
        path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", "season.nfo")
        backdrop_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", f"backdrop.jpg")
        banner_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", f"banner.jpg")

    else: # No season indicator, defaulting to show overview
        tvshow = ET.Element("tvshow")

        ET.SubElement(tvshow, "title").text = safe_unicode(anilist_json.get("title", {}).get("english", ""))
        ET.SubElement(tvshow, "year").text = safe_unicode(str(anilist_json.get("startDate", {}).get("year", "")))
        ET.SubElement(tvshow, "plot").text = safe_unicode(anilist_json.get("description", ""))
        ET.SubElement(tvshow, "rating").text = safe_unicode(str(anilist_json.get("averageScore", "")))
        ET.SubElement(tvshow, "thumb", {"aspect": "poster"}).text = safe_unicode(poster)
        # ET.SubElement(tvshow, "thumb", {"aspect": "banner"}).text = safe_unicode(anilist_json.get("bannerImage", ""))
        tree = ET.ElementTree(tvshow)
        path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "tvshow.nfo")
        backdrop_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "backdrop.jpg")
        banner_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "banner.jpg")

    download_image(backdrop_url, backdrop_path)
    download_image(anilist_json.get("bannerImage", ""), banner_path)

    tree.write(path, encoding="utf-8", xml_declaration=True)

def write_episode_nfo(anilist_json, mal_json, SERIES_TITLE):
    try:
        TMDB_id, TMDB_obj = next(iter(mal_json.get("TMDB", "").items()))
        TMDB_id = int(TMDB_id)
        shows_arr = TMDB_obj.get("metadata", {}).get("episodes", [])

        # Find the episode number the MAL JSON labels it as
        mal_first_episode_number = int(shows_arr[0].get("number", 0))

        if mal_first_episode_number <= 0: # This only occurs if mal_last_episode_number ends up being 0
            mal_first_episode_number = 1

        mal_episode_number = mal_first_episode_number + int(EPISODE_NUMBER) - 1

        episode_obj = None
        for ep in shows_arr:
            if ep.get("number", "") == mal_episode_number:
                episode_obj = ep
                break
        
        if episode_obj is None:
            print(f"[!] Episode {mal_episode_number} not found in metadata")
            return False

    except Exception as e:
        print(f"[!] Couldn't find the key within TMDB: {e}")
        return False
    
    nfo_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER:02}", f"{SERIES_TITLE} S{SEASON_NUMBER:02}E{EPISODE_NUMBER:02}.nfo")
    episode_xml = ET.Element("episodedetails") 
    ET.SubElement(episode_xml, "title").text = safe_unicode(episode_obj.get("title", ""))
    ET.SubElement(episode_xml, "season").text = safe_unicode(str(int(SEASON_NUMBER)))
    ET.SubElement(episode_xml, "episode").text = safe_unicode(str(int(EPISODE_NUMBER)))
    ET.SubElement(episode_xml, "aired").text = safe_unicode(episode_obj.get("airDate", ""))
    ET.SubElement(episode_xml, "plot").text = safe_unicode(episode_obj.get("description", ""))
    ET.SubElement(episode_xml, "thumb", {"aspect": "poster"}).text = safe_unicode(episode_obj.get("image", ""))

    tree = ET.ElementTree(episode_xml)
    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)

def ensure_kiwi_server_selected(page):
    target_label = "dub" if DUB else "sub"
    print(f"[*] Looking for 'kiwi' server under {target_label.capitalize()} section...")

    server_groups = page.query_selector_all("div.r1s34uq0 > div")

    for group in server_groups:
        try:
            label_div = group.query_selector_all("div")[0]
            label = label_div.inner_text().strip().lower()

            if target_label in label:
                buttons = group.query_selector_all("button.b1nm6r8")
                for btn in buttons:
                    text = btn.inner_text().strip().lower()
                    if "kiwi" in text:
                        classes = btn.get_attribute("class")
                        if "active" in classes:
                            print(f"[OK] 'kiwi' server already selected under {target_label.capitalize()}.")
                        else:
                            print(f"[>] Selecting 'kiwi' server under {target_label.capitalize()}...")
                            btn.click()
                            page.wait_for_timeout(1500)
                        return
                raise Exception(f"Could not find 'kiwi' button in {target_label.capitalize()} section.")
        except Exception as e:
            print(f"[!] Error while checking server group: {e}")

    raise Exception(f"{target_label.capitalize()} section with 'kiwi' server not found.")

def get_kwik_download_link(episode: Episode_Info):
    from playwright.sync_api import sync_playwright
    import os

    kwik_f_url = episode.kwiksi_url

    print("[*] Opening kwik.si page with Playwright...")

    with sync_playwright() as p:
        user_data_dir = os.path.abspath("chromium_user_data_kwik")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=True
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(kwik_f_url)
        page.wait_for_timeout(3000)

        # Optional: Save HTML snapshot for debugging
        # html_snapshot_path = os.path.join(OUTPUT_DIR, "kwik_page_debug.html")
        # with open(html_snapshot_path, "w", encoding="utf-8") as f:
        #     f.write(page.content())
        # print(f"[OK] Saved HTML snapshot: {html_snapshot_path}")

        # Step 1: Try to close popup overlays (e.g. fake video)
        try:
            if page.query_selector("#vidmate-popup .close-popup"):
                print("[*] Found popup overlay. Closing it...")
                page.click("#vidmate-popup .close-popup")
                page.wait_for_timeout(2000)
                print("[+] Closed popup overlay.")
        except Exception as e:
            print("[!] No popup or error closing popup:", e)

        # Step 2: Try to click the "I'm not a robot" button if it appears
        try:
            if page.query_selector("button.btn.btn-primary.btn-captcha"):
                print("[*] Found human verification button. Clicking it...")
                page.click("button.btn.btn-primary.btn-captcha")
                page.wait_for_timeout(4000)
                print("[+] Clicked verification button.")
        except Exception as e:
            print("[!] No bot check button or error:", e)

        # Step 3: Poll for the download form
        print("[*] Waiting for download form to appear...")
        form_found = False
        for i in range(15):
            if page.query_selector("form[action^='https://kwik.si/d/']"):
                print("[OK] Found download form.")
                form_found = True
                break
            print(f"[{i+1}s] Still waiting...")
            time.sleep(1)

        if not form_found:
            raise Exception("Download form never appeared after verification/popup step.")

        # Step 4: Extract action and token
        form_action = page.get_attribute("form[action^='https://kwik.si/d/']", "action")
        token = page.get_attribute("input[name='_token']", "value")

        print(f"[+] Extracted form action: {form_action}")
        print(f"[+] Extracted _token: {token}")

        # Step 5: Submit form inside browser session to capture download
        print("[*] Submitting form via Playwright to get the real file...")

        for attempt in range(MAX_RETRIES):
            try:
                print(f"[*] Form submission attempt {attempt+1}/{MAX_RETRIES}")

                with page.expect_download(timeout=10000) as download_info:
                    page.evaluate(f'''
                        () => {{
                            const form = document.createElement('form');
                            form.method = 'POST';
                            form.action = '{form_action}';
                            const tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = '_token';
                            tokenInput.value = '{token}';
                            form.appendChild(tokenInput);
                            document.body.appendChild(form);
                            form.submit();
                        }}
                    ''')
                    download = download_info.value

                # Save the downloaded file
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                output_path = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
                download.save_as(output_path)

                print(f"[OK] Download complete: {output_path}")
                browser.close()
                break

            except Exception as e:
                print(f"[!] Form submission attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    print("[*] Refreshing kwik.si page to retry...")
                    page.reload()
                    page.wait_for_timeout(3000)
                else:
                    print("[X] Max download page refreshes reached. Exiting.")
                    browser.close()
                    raise

        airing = 1  # Default to airing
        if int(EPISODES_AIRED) == int(EPISODES_IN_SEASON) or int(EPISODE_NUMBER) == int(EPISODES_IN_SEASON):
            print("[*] This is the last episode of the season. Marking as not airing in the database.")
            airing = 0

        cursor.execute('''
            UPDATE episodes
            SET downloaded = 1
            WHERE miruro_id = ? AND season = ? AND episode = ?
        ''', (SERIES_ID, SEASON_NUMBER, EPISODE_NUMBER))
        cursor.execute('''
            UPDATE series
            SET last_checked = CURRENT_TIMESTAMP,
                download_failed = 0,
                is_airing = ?
            WHERE miruro_id = ?
        ''', (airing, SERIES_ID))
        conn.commit()

def create_tables():
    conn = sqlite3.connect("hue.db")
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
    conn.close()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a miruro.to episode through pahe.win ➜ kwik.si "
                    "using Playwright (Chromium + uBlock Origin)."
    )
    parser.add_argument("url", help="Full miruro episode URL")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging and open a visible (head-full) browser",
    )
    parser.add_argument(
        "--episode",
        type=int,
        help="Specify episode number to download (default: last in URL)",
    )
    parser.add_argument(
        "--episodes",
        type=str,
        help=f"Specify a range of episodes to download (e.g. 1-5) Max {MAX_EPISODES} episodes"
    )
    parser.add_argument(
        "--dub",
        action="store_true",
        help="Use the dubbed version of the episode (if available)"
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Follow the series and download new episodes as they release"
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("The url argument is required. Please provide a valid miruro.to episode link.")

    return args

def trigger_jellyfin_scan():
    url = "http://localhost:8096/Library/Refresh"
    headers = {
        "X-Emby-Token": JELLYFIN_API_KEY
    }
    response = requests.post(url, headers=headers)
    if response.status_code == 204:
        print("[OK] Jellyfin library scan initiated.")
    else:
        print(f"[X] Failed to start Jellyfin scan. Status: {response.status_code}")

def main() -> None:
    lock_file = acquire_download_lock()
    try:
        # global config, OUTPUT_DIR, EPISODE_NUMBER, MAX_EPISODES, DUB, FOLLOW, SERIES_ID, conn, cursor
        global config
        config = Config(CONFIG_PATH)
        #MAX_EPISODES = config.get("maxEpisodes", MAX_EPISODES)

        # Create the DB tables if they don't exist yet
        create_tables()

        # Parse the command line args
        args = parse_args()

        # Create an instance of the Show_Info class
        show_info = Show_Info(args.url, args.follow)

        show_info.dub = args.dub

        # Assign the correct values based on the link and args to .episode_number and .episode_range
        show_info.confirm_episodes(args)  
        
        print(f"[*] Downloading episodes {show_info.episode_range[0]} to {show_info.episode_range[1]}")

        for episode in range(show_info.episode_range[0], show_info.episode_range[1]+1):
            for i in range(config.max_retries):
                try:
                    show_info.episode_number = episode
                    show_info.formatted_episode_number = f"{episode:02}"
                    show_info.miruro_url =  show_info.miruro_url.rsplit("&ep=", 1)[0]
                    show_info.miruro_url = f"{show_info.miruro_url}&ep={episode}"

                    episode_info = Episode_Info(show_info, episode)
                    print(f"Miruro URL: {show_info.miruro_url}")
                    print(f"Downloading episode {episode}")
                    downloadable = get_kwik_download_page(episode_info, config)
                    if downloadable == "skip": break
                    if not downloadable: continue
                    get_kwik_download_link(episode_info)
                    saved = os.path.join(config.output_dir, episode_info.output_name)
                    print(f"\n[OK] Done! File saved to: {saved}\n")
                    break  # Exit retry loop on success
                except KeyboardInterrupt:
                    print("\n[!] Cancelled by user.")
                    conn.close()
                    sys.exit(3) # 3 for user cancellation
                except Exception as exc:  # pylint: disable=broad-except
                    print(f"\n[!] Error: {exc}\n")
                    if args.debug:
                        raise
                if i == MAX_RETRIES - 1:
                    print("Max retries reached. Exiting.")
                    try:
                        cursor.execute('''
                            UPDATE series
                            SET download_failed = 1
                            WHERE miruro_id = ?
                        ''', (SERIES_ID,))
                        conn.commit()
                    except Exception as e:
                        print(f"Error setting download_failed to true: {e}")
                    conn.close()
                    sys.exit(2) # 2 for download failure
                print(f"Retrying... Attempt ({i+2}/{MAX_RETRIES}) in {config.get('retryDelay', 5)} seconds...")
                time.sleep(config.get("retryDelay", 5))
    finally:

        portalocker.unlock(lock_file)
        lock_file.close()
        print("[*] Download process completed. Lock released.")

if __name__ == "__main__":
    main()
