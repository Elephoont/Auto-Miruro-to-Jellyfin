from playwright.sync_api import sync_playwright, TimeoutError
import time
import json
import argparse
import os
import re
import sys

# TODO: Add support for other streaming servers with download links if they are added to the site
# TODO: Add a way to enable developer mode on first run by committing a seed chromium profile with only preferences

# Path to unpacked uBlock Origin extension
UBLOCK_PATH = os.path.abspath("./uBlock0.chromium")

# Output directory for downloaded video
OUTPUT_DIR = os.path.abspath("./output") # Default if not set in config
OUTPUT_NAME = "episode.mp4"
SERIES_TITLE = "Unknown Series"
SEASON_NUMBER = 1
EPISODE_NUMBER = 0
EPISODE_NAME = "Unknown Episode"
MAX_RETRIES = 3
MAX_EPISODES = 25  # Maximum episodes to download in one run
DUB = False  # Default to subbed unless specified
EPISODES_IN_SEASON = 0 # Number of episodes in the selected season for range validation
EPISODES_AIRED = 0
config = {}

def get_kwik_download_page(miruro_url):
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

        # Get basic episode and series information
        gather_episode_info(page)

        # Check if the requested episode number is valid
        if int(EPISODE_NUMBER) not in range(1, EPISODES_AIRED + 2):  # +2 because the aired count is sometimes off by 1
            if int(EPISODE_NUMBER) not in range(1, EPISODES_IN_SEASON + 1):
                print(f"✖ Error: Episode {int(EPISODE_NUMBER)} is not valid for this season. "
                    f"Only episodes 1 to {EPISODES_IN_SEASON} are available in this season.")
                sys.exit(1)
            else:
                print(f"✖ Error: Episode {int(EPISODE_NUMBER)} has not aired yet. "
                    f"Only episodes 1 to {EPISODES_AIRED + 1 if EPISODES_AIRED + 1 <= EPISODES_IN_SEASON else EPISODES_AIRED} have aired.")
                sys.exit(1)

        # Check if the page is on the correct episode
        ep_number_element = page.query_selector(".title-container .ep-number")
        if not ep_number_element:
            raise ValueError("Could not find episode number on page.")
        match = re.search(r'\d+', ep_number_element.inner_text().strip())
        ep_number_element = int(match.group(0))
        if ep_number_element != int(EPISODE_NUMBER):
            raise ValueError(f"Current episode ({ep_number_element}) does not match requested episode ({EPISODE_NUMBER}). "
                             "Please check the URL or specify the episode with --episode or --episodes.")

        # Check if the correct playpack server is selected
        print("[*] Checking if playback server is Kiwi...")
        ensure_kiwi_server_selected(page)
        print("[✓] Kiwi server is selected under Sub section.")

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
                    print(f"[✓] Found kwik.si URL: {href}")
                    browser.close()
                    return href
            else:
                print(f"[{i+1:02}s] 'a.redirect' not yet found.")
            new_page.wait_for_timeout(1000)

        browser.close()
        raise Exception("Timed out waiting for redirect button.")

def gather_episode_info(page):
    global SERIES_TITLE, EPISODE_NAME, SEASON_NUMBER, EPISODE_NUMBER, OUTPUT_NAME, EPISODES_IN_SEASON, EPISODES_AIRED
    SERIES_TITLE = page.query_selector("div.title.anime-title a").inner_text()
    if DUB:
        SERIES_TITLE += " (Dubbed)"

    if not EPISODES_IN_SEASON:
        info_blocks = page.query_selector_all("div.t4mg1tz p")
        for block in info_blocks:
            text = block.inner_text().strip()
            if "Episodes" in text:
                match = re.search(r'(\d+\s*/\s*\d+)', text)
                if match:
                    episodes = match.group(1)
                    break
        print(f"[+] {episodes} episodes found in the series info block.")
        if episodes:
            episodes = episodes.split('/')
            EPISODES_AIRED = int(episodes[0].strip())
            EPISODES_IN_SEASON = int(episodes[1].strip())

    EPISODE_NAME = page.query_selector(".title-container .ep-title").inner_text()

    match = re.match(r'^(.*?)(?:\s+(Season\s+\d+))?(?:\s+Part\s*\d+|\s+Cour\s*\d+)?$', SERIES_TITLE, re.IGNORECASE)
    if match:
        series = match.group(1).strip()
        season = match.group(2).strip().replace("Season ", "") if match.group(2) else None
        SERIES_TITLE = series
        SEASON_NUMBER = int(season) if season else 1

    SEASON_NUMBER = f"{SEASON_NUMBER:02}"
    EPISODE_NUMBER = f"{EPISODE_NUMBER:02}"

    FILENAME = f"{SERIES_TITLE} S{SEASON_NUMBER}E{EPISODE_NUMBER}.mp4"
    OUTPUT_NAME = os.path.join(SERIES_TITLE, f"Season {SEASON_NUMBER}", FILENAME)

    print(f"[+] Series: {SERIES_TITLE} | Season: {SEASON_NUMBER} | Episode: {EPISODE_NAME}")


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
                            print(f"[✓] 'kiwi' server already selected under {target_label.capitalize()}.")
                        else:
                            print(f"[→] Selecting 'kiwi' server under {target_label.capitalize()}...")
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
        # print(f"[📄] Saved HTML snapshot: {html_snapshot_path}")

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
                print("[✓] Found download form.")
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

        with page.expect_download() as download_info:
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

        print(f"[✅] Download complete: {output_path}")
        browser.close()

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
    args = parser.parse_args()
    if not args.url:
        parser.error("The url argument is required. Please provide a valid miruro.to episode link.")

    return args

CONFIG_PATH = "config.json"

def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found at {path}")
    with open(path, "r") as file:
        return json.load(file)

def main() -> None:
    global config, OUTPUT_DIR, EPISODE_NUMBER, MAX_EPISODES, DUB
    config = load_config()
    MAX_EPISODES = config.get("maxEpisodes", MAX_EPISODES)

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
    
    for episode in range(EPISODE_RANGE[0], EPISODE_RANGE[1]+1):
        for i in range(MAX_RETRIES):
            try:
                Miruro_URL =  Miruro_URL.rsplit("&ep=", 1)[0]
                Miruro_URL = f"{Miruro_URL}&ep={episode}"
                print(f"Miruro URL: {Miruro_URL}")
                print(f"Downloading episode {episode} of {EPISODE_RANGE[1]}")
                kwik_f_url = get_kwik_download_page(Miruro_URL)
                get_kwik_download_link(kwik_f_url)
                saved = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
                print(f"\n✅ Done! File saved to: {saved}\n")
                break  # Exit retry loop on success
            except KeyboardInterrupt:
                print("\n[!] Cancelled by user.")
                return
            except Exception as exc:  # pylint: disable=broad-except
                print(f"\n✖ Error: {exc}\n")
                if args.debug:
                    raise
            if i == MAX_RETRIES - 1:
                print("Max retries reached. Exiting.")
                return
            print(f"Retrying... Attempt ({i+2}/{MAX_RETRIES}) in {config.get('retryDelay', 5)} seconds...")
            time.sleep(config.get("retryDelay", 5))


# def main():
#     miruro_url = input("Paste your miruro.to episode link: ").strip()
#     kwik_f_url = get_kwik_download_page(miruro_url)
#     get_kwik_download_link(kwik_f_url)


if __name__ == "__main__":
    main()
