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

warnings.filterwarnings("ignore", message="The default datetime adapter is deprecated*", category=DeprecationWarning)

# TODO: Add support for other streaming servers with download links if they are added to the site
# TODO: Add a way to enable developer mode on first run by committing a seed chromium profile with only preferences
# TODO: Add support for shows that havent begun airing

# Load jellyfin API key from .env file
load_dotenv()
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY")

# Path to unpacked uBlock Origin extension
UBLOCK_PATH = os.path.abspath("./uBlock0.chromium")

# Output directory for downloaded video
OUTPUT_DIR = os.path.abspath("./output") # Default if not set in config
OUTPUT_NAME = "episode.mp4"
SERIES_TITLE = "Unknown Series"
SERIES_ID = None
SEASON_NUMBER = 1
EPISODE_NUMBER = 0
EPISODE_NAME = "Unknown Episode"
FOLLOW = False  # Whether to follow the series and download new episodes as they release
MAX_RETRIES = 3
MAX_EPISODES = 25  # Maximum episodes to download in one run
DUB = False  # Default to subbed unless specified
EPISODES_IN_SEASON = 0 # Number of episodes in the selected season for range validation
EPISODES_AIRED = 0
AIRING = False
CONFIG_PATH = "config.json"
config = {}
conn = None
cursor = None
LOCK_FILE = "download.lock"

def acquire_download_lock():
    lock_file = open(LOCK_FILE, "w")
    print("[*] Waiting to acquire download lock...")
    portalocker.lock(lock_file, portalocker.LOCK_EX)  # Will block here until lock is free
    print("[OK] Lock acquired.")
    return lock_file

def get_kwik_download_page(miruro_url):
    # Before opening the browser, check if the episode has already been downloaded
    cursor.execute('''
        SELECT downloaded FROM episodes
        WHERE miruro_id = ? AND episode = ? AND downloaded = 1
    ''', (SERIES_ID, EPISODE_NUMBER))
    row = cursor.fetchone()
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
            return "skip"
        print("[*] Database indicates episode is downloaded, but file does not exist. ")
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

        # Now write .nfo files to ensure jellyfin has reliable metadata
        parse_metadata(page, browser, SERIES_ID)

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

def gather_episode_info(page, browser):
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

def parse_metadata(page, browser, miruro_id):
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
    write_series_nfo(anilist_json, mal_json)
    write_episode_nfo(anilist_json, mal_json)
    return

def write_series_nfo(anilist_json, mal_json): # info, episodes (respectively)
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

        ET.SubElement(season, "title").text = anilist_json.get("title", {}).get("english", "")
        ET.SubElement(season, "seasonnumber").text = str(int(SEASON_NUMBER))
        ET.SubElement(season, "year").text = str(anilist_json.get("startDate", {}).get("year", ""))
        ET.SubElement(season, "plot").text = anilist_json.get("description", "")
        ET.SubElement(season, "rating").text = str(anilist_json.get("averageScore", ""))
        ET.SubElement(season, "thumb", {"aspect": "poster"}).text = poster
        # ET.SubElement(season, "thumb", {"aspect": "banner"}).text = anilist_json.get("bannerImage", "")
        tree = ET.ElementTree(season)
        path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", "season.nfo")
        backdrop_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", f"season{SEASON_NUMBER}-backdrop.jpg")
        banner_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, f"Season {SEASON_NUMBER}", f"season{SEASON_NUMBER}-banner.jpg")

    else: # No season indicator, defaulting to show overview
        tvshow = ET.Element("tvshow")

        ET.SubElement(tvshow, "title").text = anilist_json.get("title", {}).get("english", "")
        ET.SubElement(tvshow, "year").text = str(anilist_json.get("startDate", {}).get("year", ""))
        ET.SubElement(tvshow, "plot").text = anilist_json.get("description", "")
        ET.SubElement(tvshow, "rating").text = str(anilist_json.get("averageScore", ""))
        ET.SubElement(tvshow, "thumb", {"aspect": "poster"}).text = poster
        # ET.SubElement(tvshow, "thumb", {"aspect": "banner"}).text = anilist_json.get("bannerImage", "")
        tree = ET.ElementTree(tvshow)
        path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "tvshow.nfo")
        backdrop_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "backdrop.jpg")
        banner_path = os.path.join(OUTPUT_DIR, SERIES_TITLE, "banner.jpg")

    download_image(backdrop_url, backdrop_path)
    download_image(anilist_json.get("bannerImage", ""), banner_path)

    tree.write(path, encoding="utf-8", xml_declaration=True)

def write_episode_nfo(anilist_json, mal_json):
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
    ET.SubElement(episode_xml, "title").text = episode_obj.get("title", "")
    ET.SubElement(episode_xml, "season").text = str(int(SEASON_NUMBER))
    ET.SubElement(episode_xml, "episode").text = str(int(EPISODE_NUMBER))
    ET.SubElement(episode_xml, "aired").text = episode_obj.get("airDate", "")
    ET.SubElement(episode_xml, "plot").text = episode_obj.get("description", "")
    ET.SubElement(episode_xml, "thumb", {"aspect": "poster"}).text = episode_obj.get("image", "")

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

def get_kwik_download_link(kwik_f_url):
    from playwright.sync_api import sync_playwright
    import os

    if kwik_f_url == "skip":
        return

    print("[*] Opening kwik.si page with Playwright...")

    with sync_playwright() as p:
        user_data_dir = os.path.abspath("chromium_user_data_kwik")
        browser = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=[
                f'--disable-extensions-except={UBLOCK_PATH}',
                f'--load-extension={UBLOCK_PATH}',
            ]
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

def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found at {path}")
    with open(path, "r") as file:
        return json.load(file)

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
        global config, OUTPUT_DIR, EPISODE_NUMBER, MAX_EPISODES, DUB, FOLLOW, SERIES_ID, conn, cursor
        config = load_config()
        MAX_EPISODES = config.get("maxEpisodes", MAX_EPISODES)

        conn = sqlite3.connect("hue.db")
        cursor = conn.cursor()
        create_tables()

        args = parse_args()
        Miruro_URL = args.url    
        DUB = args.dub
        
        OUTPUT_DIR = os.path.abspath(config.get("outputDir", OUTPUT_DIR))

        if args.episode:
            EPISODE_NUMBER = args.episode
            EPISODE_RANGE = (EPISODE_NUMBER, EPISODE_NUMBER)
        elif args.episodes and '-' in args.episodes:
            EPISODE_RANGE = args.episodes.split('-')
            if len(EPISODE_RANGE) != 2 or not all(x.isdigit() for x in EPISODE_RANGE):
                raise ValueError("Invalid episode range format. Use 'start-end' (e.g. 1-5).")
            EPISODE_RANGE = (int(EPISODE_RANGE[0]), int(EPISODE_RANGE[1]))
            if EPISODE_RANGE[0] <= 0 or EPISODE_RANGE[1] <= 0:
                raise ValueError("Episode numbers must be positive integers.")
            if EPISODE_RANGE[0] > EPISODE_RANGE[1]:
                raise ValueError("Start episode must be less than or equal to end episode.")
            if EPISODE_RANGE[1] - EPISODE_RANGE[0] + 1 > MAX_EPISODES:
                raise ValueError(f"Cannot download more than {MAX_EPISODES} episodes at once.")
        else:
            # Ensure the URL contains &ep=NUM at the end
            if Miruro_URL.rsplit("&ep=", 1)[-1].isdigit():
                EPISODE_NUMBER = int(re.sub("[^0-9]", "", args.url[-4:]))
                if EPISODE_NUMBER <= 0:
                    raise ValueError("Could not determine episode number from URL. "
                                    "Please specify with --episode or --episodes.")
                EPISODE_RANGE = (EPISODE_NUMBER, EPISODE_NUMBER)
            else:
                raise ValueError("No episode number found in URL. "
                                "Please specify with --episode or --episodes.")
        
        SERIES_ID = re.search(r'id=(\d+)', Miruro_URL)
        if SERIES_ID:
            SERIES_ID = SERIES_ID.group(1)
            print(f"[*] Series ID: {SERIES_ID}")
        else:
            print("[!] Could not determine series ID from URL. "
                "Please ensure the URL is correct and contains a valid series ID.")
            conn.close()
            sys.exit(1)

        if args.follow:
            FOLLOW = True
            print("[*] Following the series for new episodes...")
        
        print(f"[*] Downloading episodes {EPISODE_RANGE[0]} to {EPISODE_RANGE[1]}")

        for episode in range(EPISODE_RANGE[0], EPISODE_RANGE[1]+1):
            for i in range(MAX_RETRIES):
                try:
                    EPISODE_NUMBER = episode
                    Miruro_URL =  Miruro_URL.rsplit("&ep=", 1)[0]
                    Miruro_URL = f"{Miruro_URL}&ep={episode}"
                    print(f"Miruro URL: {Miruro_URL}")
                    print(f"Downloading episode {episode}")
                    kwik_f_url = get_kwik_download_page(Miruro_URL)
                    get_kwik_download_link(kwik_f_url)
                    saved = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
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
