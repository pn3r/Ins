import os
import random
import time
import json
import sys
import contextlib
import logging
import datetime
from configparser import ConfigParser
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
from colorama import init, Fore, Style
init()
n = Fore.RESET
lg = Fore.LIGHTGREEN_EX
r = Fore.RED
w = Fore.WHITE
cy = Fore.CYAN
ye = Fore.YELLOW

from alive import alive

alive()

@contextlib.contextmanager
def suppress_stdout_stderr():
    with open(os.devnull, 'w') as fnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = fnull
        sys.stderr = fnull
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

config = ConfigParser()
config.read("setting.conf")
settings = config["DEFAULT"]
USERNAME = settings["username"]
PASSWORD = settings["password"]

within_day_setting = settings["Within_day"]
if "days" in within_day_setting or "day" in within_day_setting:
    days_str = ''.join(c for c in within_day_setting if c.isdigit())
    WITHIN_DAY = int(days_str) * 24 * 3600
else:
    WITHIN_DAY = int(within_day_setting)

get_post_delay = tuple(map(int, settings["get_post_delay"].split(",")))
LIKE_DELAY = tuple(map(float, settings["Like_delay"].split(",")))
TIMEZONE_OFFSET = int(settings["timezone"])
get_min_post = int(settings["get_min_post"])
get_max_post = int(settings["get_max_post"])
MIN_LIKE = int(settings["min_like"])
MAX_LIKE = int(settings["max_like"])
follow_cycle_range = tuple(map(int, settings["follow_cycle_range"].split(",")))
comment_chance = int(settings.get("comment_chance", "0").strip('%')) / 100
ENABLE_COMMENTING = settings.get("enable_commenting", "n").lower() == "y"

def parse_time_range(time_str):
    parts = time_str.split('/')
    result = []
    
    for part in parts:
        time_val = ''.join(c for c in part if c.isdigit() or c == '.')
        
        is_pm = 'pm' in part.lower()
        is_am = 'am' in part.lower()
        
        time_float = float(time_val)
        
        if is_pm and time_float < 12:
            time_float += 12
        elif is_am and time_float == 12:
            time_float = 0
            
        result.append(time_float)
    
    return tuple(result)

def parse_cycle_range(cycle_str):
    parts = cycle_str.split('/')
    return tuple(map(int, parts))

schedules = []
startup_schedule = None

if "cycles0" in settings:
    cycle_range = parse_cycle_range(settings["cycles0"])
    startup_schedule = {
        "name": "startup",
        "time_range": None,
        "cycle_range": cycle_range
    }

for i in range(1, 9):
    if f"start{i}" in settings and f"cycles{i}" in settings:
        time_range = parse_time_range(settings[f"start{i}"])
        cycle_range = parse_cycle_range(settings[f"cycles{i}"])
        schedules.append({
            "name": f"schedule{i}",
            "time_range": time_range,
            "cycle_range": cycle_range
        })

COMMENT_FILE = "comments.txt"
LOCK_FILE = "log.json"
LOCK_DURATION = 2 * 3600

def load_user_agent_and_device():
    user_agent_file = "userAgent.txt"
    
    default_device = {
        "app_version": "291.0.0.30.120",
        "android_version": 33,
        "android_release": "13",
        "dpi": "440dpi",
        "resolution": "1080x2400",
        "manufacturer": "Xiaomi",
        "device": "2312DRA50G",
        "model": "Redmi Note 13",
        "cpu": "mediatek",
        "version_code": "353166753",
    }
    
    default_ua = (
        "Instagram 291.0.0.30.120 Android (33/13; 440dpi; 1080x2400; Xiaomi/xiaomi; "
        "Redmi Note 13; 2312DRA50G; mediatek; en_US; 353166753)"
    )
    
    if not os.path.exists(user_agent_file):
        print(f"{w}Using default {r}(userAgent.txt {w}not found){n}")
        return default_ua, default_device
    
    try:
        with open(user_agent_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        
        if not lines:
            print(f"{r}userAgent.txt is empty, {w}using defaults{n}")
            return default_ua, default_device
        
        user_agent = lines[0]
        
        device_settings = default_device.copy()
        
        if len(lines) > 1:
            for line in lines[1:]:
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    if key in ['android_version', 'version_code']:
                        try:
                            device_settings[key] = int(value)
                        except ValueError:
                            device_settings[key] = value
                    else:
                        device_settings[key] = value
            
            print(f" ")
        else:
            print(f" ")
        
        return user_agent, device_settings
        
    except Exception as e:
        print(f"{w}Error reading {r}userAgent.txt: {e}{n}")
        print(f"{w}Using default user agent and device settings{n}")
        return default_ua, default_device

USER_AGENT, DEVICE_SETTINGS = load_user_agent_and_device()

def load_comments():
    if os.path.exists(COMMENT_FILE):
        with open(COMMENT_FILE, "r", encoding="utf-8") as f:
            comments = [line.strip() for line in f if line.strip()]
            if comments:
                return comments
    return []

COMMENTS = load_comments()

enable_commenting = ENABLE_COMMENTING
if COMMENTS and enable_commenting:
    print(f"{w}Commenting is enabled{n}")
else:
    if not COMMENTS:
        print(f"{r}comments.txt is missing or empty.. {w}Starting in like-only mode{n}")
    else:
        print(f"{w}Commenting is disabled{n}")

def load_locks():
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r") as f:
            try:
                content = f.read().strip()
                return json.loads(content) if content else {}
            except json.JSONDecodeError:
                print(f"{r} Warning: log.json is invalid.. Resetting{n}")
                return {}
    return {}

def save_locks(locks):
    with open(LOCK_FILE, "w") as f:
        json.dump(locks, f)

def is_locked(post_id, user_id, media_id, locks):
    now = time.time()
    for key in [post_id, user_id, media_id]:
        if key in locks and now - locks[key] < LOCK_DURATION:
            return True
    return False

def update_locks(post_id, user_id, media_id, locks):
    now = time.time()
    for key in [post_id, user_id, media_id]:
        locks[key] = now

def get_client():
    client = Client()
    session_file = "session.json"

    client.set_device(DEVICE_SETTINGS)
    client.set_user_agent(USER_AGENT)
    client.set_locale('en_US')
    client.set_timezone_offset(TIMEZONE_OFFSET)

    if os.path.exists(session_file):
        try:
            client.load_settings(session_file)
            client.get_timeline_feed()
            print(f"{cy} Session loaded and authorized{n}")
            return client
        except Exception:
            print(f"{r} Session failed.. Logging in again{n}")

    client.login(USERNAME, PASSWORD)
    print(f"{cy} Logged in successfully{n}")
    client.dump_settings(session_file)
    print(f"{cy} Session saved{n}")
    return client

def format_instagram_time(timestamp):
    time_diff = time.time() - timestamp
    days = time_diff // (24 * 3600)
    hours = (time_diff % (24 * 3600)) // 3600
    minutes = (time_diff % 3600) // 60
    seconds = time_diff % 60

    if days > 0:
        if days == 1:
            return "Yesterday"
        else:
            return f"{int(days)} days ago"
    elif hours > 0:
        return f"{int(hours)} hours ago"
    elif minutes > 0:
        return f"{int(minutes)} minutes ago"
    else:
        return f"{int(seconds)} seconds ago"

def like_feed_posts(client, get_min_post=get_min_post, get_max_post=get_max_post, min_like=MIN_LIKE, max_like=MAX_LIKE):
    locks = load_locks()
    try:
        feed = client.get_timeline_feed()
        feed_items = feed.get('feed_items', [])
        media_posts = []

        for item in feed_items:
            media = item.get('media_or_ad')
            if not media:
                continue
            if 'pk' not in media or 'user' not in media:
                continue

            media_posts.append({
                "pk": str(media["pk"]),
                "user_id": str(media["user"]["pk"]),
                "taken_at": media.get("taken_at"),
                "has_liked": media.get("has_liked", False)
            })

        fetch_count = random.randint(get_min_post, get_max_post)
        if not media_posts:
            print(f"{r}⚠️ No feed media found{n}")
            return

        feed_sample = random.sample(media_posts, min(fetch_count, len(media_posts)))
        print(f"{w} Fetched: {r}{len(feed_sample)} {w}media items from feed{n}")

        now = time.time()
        unliked = []
        post_times = {}

        for post in feed_sample:
            post_id = post["pk"]
            user_id = post["user_id"]
            taken_at = post.get("taken_at") or now

            upload_time_str = format_instagram_time(taken_at)
            post_times[post_id] = upload_time_str

            if not post["has_liked"] and not is_locked(post_id, user_id, post_id, locks):
                if now - taken_at <= WITHIN_DAY:
                    unliked.append(post)
                    print(f"{w} recent: {r}{post_id} {w}{upload_time_str}{n}")
                else:
                    print(f"{r} ignored: {w}{post_id} {r}{upload_time_str}{n}")
            else:
                print(f"{r} ignored: {w}{post_id} {r}{upload_time_str} {w}already liked{n}")

        if not unliked:
            print(f"{r} No posts to like{n}")
            return

        max_possible = min(max_like, len(unliked))
        if max_possible < min_like:
            print(f"{r} Not enough recent posts to like{n}")
            return

        like_count = random.randint(min_like, max_possible)
        selected = random.sample(unliked, like_count)

        for post in selected:
            post_id = post["pk"]
            user_id = post["user_id"]
            delay = random.uniform(*LIKE_DELAY)
            time.sleep(delay)

            try:
                try:
                    media_id = client.media_id(post_id)
                except Exception:
                    continue

                try:
                    client.media_like(media_id)
                    print(f"{w} ❤︎ Liked: {r}{post_id} {w}{post_times[post_id]}{n}")
                    update_locks(post_id, user_id, media_id, locks)
                except Exception as e:
                    if "validation" in str(e).lower():                    	
                        continue
                    else:
                        print(f"{r} ✘ Failed to like {post_times[post_id]}: {str(e).splitlines()[0]}{n}")
                        continue

                if enable_commenting and random.random() < comment_chance and COMMENTS:
                    comment_text = random.choice(COMMENTS)
                    try:
                        client.media_comment(media_id, comment_text)
                        print(f"{w} 💬 Commented on: {r}{post_times[post_id]} -> {w}{comment_text}{n}")
                    except Exception:
                        print(f"{r} Failed to comment on: {w}{post_times[post_id]}{n}")

            except Exception as e:
                print(f"{r} ✘ Unexpected error on {w}{post_times[post_id]}: {r}{str(e).splitlines()[0]}{n}")

        save_locks(locks)

    except LoginRequired:
        print(f"{r} Login required.. Session expired. exiting..!{n}")
        sys.exit(1)
    except Exception as e:
        print(f"{r} Error while liking posts: {w}{e}{n}")
        sys.exit(1)

def load_usernames(file_path="username.txt"):
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return []

def save_usernames(usernames, file_path="username.txt"):
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(usernames))

def append_followed(username, file_path="followed.txt"):
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(f"{username}\n")

def follow_one_user(client):
    usernames = load_usernames()
    if not usernames:
        print(f"{w} No more users to follow!{n}")
        return

    username = usernames.pop(0)
    save_usernames(usernames)

    try:
        with suppress_stdout_stderr():
            user_id = client.user_info_by_username(username).pk
            client.user_follow(user_id)
        append_followed(username)
        print(f"{w} Followed user: {r}@{username}{n}")
    except LoginRequired:
        print(f"{r} Login required during follow.. Session expired.. exiting..!{n}")
        sys.exit(1)
    except Exception:
        print(f"{r} ignoring follow user due to fetch error: {w}@{username}{n}")

def countdown_sleep(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    for remaining in range(seconds, 0, -1):
        h = remaining // 3600
        m = (remaining % 3600) // 60
        s = remaining % 60
        print(f"{w}  sleep for {cy}{h:02d}:{lg}{m:02d}:{r}{s:02d}{w}...!", end="\r", flush=True)
        time.sleep(1)
    print(f" " * 60, end="\r")

def choose_random_start_time(schedule):
    time_range = schedule["time_range"]
    if len(time_range) == 1:
        return time_range[0]
    elif len(time_range) == 2:
        start_hour, end_hour = sorted(time_range)
        random_hours = random.uniform(start_hour, end_hour)
        return random_hours
    return None

def choose_random_cycles(schedule):
    cycle_range = schedule["cycle_range"]
    if len(cycle_range) == 2:
        return random.randint(cycle_range[0], cycle_range[1])
    return cycle_range[0]

def is_time_in_range(current_hour, time_range):
    if len(time_range) == 2:
        start_hour, end_hour = sorted(time_range)
        if start_hour > end_hour:
            return current_hour >= start_hour or current_hour <= end_hour
        else:
            return start_hour <= current_hour <= end_hour
    elif len(time_range) == 1:
        target_hour = time_range[0]
        return abs(current_hour - target_hour) <= 0.5
    return False

def get_current_matching_schedule():
    current_time = datetime.datetime.now()
    current_hour = current_time.hour + (current_time.minute / 60)
    
    for schedule in schedules:
        if is_time_in_range(current_hour, schedule["time_range"]):
            cycles = choose_random_cycles(schedule)
            return {
                "name": schedule["name"],
                "cycles": cycles,
                "matched": True
            }
    
    return None

def get_next_schedule():
    current_time = datetime.datetime.now()
    current_hour = current_time.hour + (current_time.minute / 60)
    
    nearest_schedule = None
    min_wait_time = float('inf')
    
    for schedule in schedules:
        time_range = schedule["time_range"]
        if len(time_range) >= 1:
            target_hour = time_range[0]
            
            if target_hour > current_hour:
                wait_hours = target_hour - current_hour
            else:
                wait_hours = (24 - current_hour) + target_hour
            
            if wait_hours < min_wait_time:
                min_wait_time = wait_hours
                nearest_schedule = schedule
    
    if nearest_schedule:
        start_time = choose_random_start_time(nearest_schedule)
        cycles = choose_random_cycles(nearest_schedule)
        return {
            "name": nearest_schedule["name"],
            "start_time": start_time,
            "cycles": cycles
        }
    
    return None

def calculate_wait_time(target_hour):
    current_time = datetime.datetime.now()
    current_hour = current_time.hour + (current_time.minute / 60)
    
    if target_hour > current_hour:
        wait_hours = target_hour - current_hour
    else:
        wait_hours = (24 - current_hour) + target_hour
        
    return int(wait_hours * 3600)

if __name__ == "__main__":
    print(f"{w}Feed Liker bot started{n}")
    print(f"{w}Using Bangladesh timezone (UTC+6){n}")

    first_run = True

    while True:
        if first_run:
            if startup_schedule:
                total_cycles = choose_random_cycles(startup_schedule)
                print(f"{w}⚡ Running STARTUP session {r}(cycles0) {w}with {r}{total_cycles} {w}cycles ⚡{n}")
                
                try:
                    client = get_client()
                    cycle_count = 0
                    next_follow_in = random.randint(*follow_cycle_range)

                    current_cycle = 0
                    while current_cycle < total_cycles:
                        current_cycle += 1
                        cycle_count += 1

                        print(f"{w} ▶ Running cycle {r}{current_cycle}{w}/{r}{total_cycles}{n}")
                        like_feed_posts(client)

                        if cycle_count >= next_follow_in:
                            follow_one_user(client)
                            cycle_count = 0
                            next_follow_in = random.randint(*follow_cycle_range)

                        if current_cycle < total_cycles:
                            delay = random.randint(*get_post_delay)
                            countdown_sleep(delay)

                    print(f"{w} Startup session complete!{n}")
                    
                except KeyboardInterrupt:
                    print(f"{r}✘ Bot manually stopped by user..!{n}")
                    break
                except Exception as e:
                    print(f"{r}⚠️ Error occurred: {w}{e}{n}")
                    print(f"{r}Restarting in 10 minutes..!{n}")
                    countdown_sleep(600)
                    continue
            
            first_run = False
            
            current_match = get_current_matching_schedule()
            if current_match:
                print(f"{w}🎯 Current time matches {r}{current_match['name']}! {n}")
                total_cycles = current_match["cycles"]
            else:
                next_schedule = get_next_schedule()
                if next_schedule:
                    wait_seconds = calculate_wait_time(next_schedule["start_time"])
                    hours = int(next_schedule["start_time"])
                    minutes = int((next_schedule["start_time"] - hours) * 60)
                    print(f"{w}⏰ Next session {r}({next_schedule['name']}) {w}scheduled at {cy}{hours:02d}{w}:{r}{minutes:02d} {w}with {r}{next_schedule['cycles']} {w}cycles{n}")
                    if wait_seconds > 60:
                        countdown_sleep(wait_seconds)
                    total_cycles = next_schedule["cycles"]
                else:
                    print(f"{r}💤 No schedules found.. waiting 1 hour..!{n}")
                    countdown_sleep(3600)
                    continue
        else:
            current_match = get_current_matching_schedule()
            
            if current_match:
                print(f"{w}🎯 Current time matches {r}{current_match['name']}! {n}")
                total_cycles = current_match["cycles"]
            else:
                next_schedule = get_next_schedule()
                if not next_schedule:
                    print(f"{r}💤 no valid schedules found.. waiting 1 hour before checking again{n}")
                    countdown_sleep(3600)
                    continue

                wait_seconds = calculate_wait_time(next_schedule["start_time"])
                hours = int(next_schedule["start_time"])
                minutes = int((next_schedule["start_time"] - hours) * 60)
                print(f"{w}⏰ Next session {r}({next_schedule['name']}) {w}scheduled at {cy}{hours:02d}{w}:{r}{minutes:02d} {w}with {r}{next_schedule['cycles']} {w}cycles{n}")

                if wait_seconds > 60:
                    countdown_sleep(wait_seconds)
                
                total_cycles = next_schedule["cycles"]

        try:
            client = get_client()
            cycle_count = 0
            next_follow_in = random.randint(*follow_cycle_range)

            print(f"{w}🚀 Starting activity session with {r}{total_cycles} {w}cycles{n}")

            current_cycle = 0
            while current_cycle < total_cycles:
                current_cycle += 1
                cycle_count += 1

                print(f"{w}▶ Running cycle {r}{current_cycle}{w}/{r}{total_cycles}{n}")
                like_feed_posts(client)

                if cycle_count >= next_follow_in:
                    follow_one_user(client)
                    cycle_count = 0
                    next_follow_in = random.randint(*follow_cycle_range)

                if current_cycle < total_cycles:
                    delay = random.randint(*get_post_delay)
                    countdown_sleep(delay)

            print(f"{w}✔ Session complete, planning next session!{n}")

        except KeyboardInterrupt:
            print(f"{r}✘ bot manually stopped by user..!{n}")
            break
        except Exception as e:
            print(f"{w}⚠️ Error: {e}{n}")
            print(f"{w}Restarting in 10 minutes..!{n}")
            countdown_sleep(600)