# Local Name: modded-replit.py

import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
import random
import time
import aiohttp
from flask import Flask
from threading import Thread
from collections import deque
import json
import os
from keep_alive import keep_alive

keep_alive()

from dotenv import load_dotenv
load_dotenv(dotenv_path="../.env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# --- Caching setup ---
cache_dir = 'cache'
os.makedirs(cache_dir, exist_ok=True)
cache = {}  # query -> local file path

# --- Queues & History per guild ---
song_queues = {}       # guild_id -> deque of (query, interaction, secret_flag)
play_history = {}      # guild_id -> deque of previously played queries

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Alt messages ---
RESPONSES = {
    "play": [
        "Yay~ Now playing: **{title}**! Enjoy the vibes!",
        "Teehee~ I queued up **{title}** just for you!",
        "Here comes **{title}**! Let’s jam together!",
        "Spinning up **{title}**~ Hope it makes you smile!",
    ],
    "pause": [
        "Pausey-wausey~ Let’s take a break!",
        "Hold on~ I’ll pause it, just for you!",
        "Alrighty~ We’re on pause now!",
        "Music break time~ Let me know when to resume!",
    ],
    "resume": [
        "Resuming the beat~ Let’s groove!",
        "Back to jamming~ Let’s gooo!",
        "Yay~ Unpaused and playing again!",
        "No more silence~ Let’s keep the fun going!",
    ],
    "skip": [
        "Oki doki~ Skipping to the next one!",
        "Next please~ Zooming ahead!",
        "Whoosh~ That song's gone, here comes the next!",
        "Let’s try something else~ Skipped!",
    ],
    "end": [
        "Music’s all done~ That was fun!",
        "All stopped~ Hope you liked it!",
        "I’ve stopped the tunes for now~",
        "That’s a wrap~ Let me know if you want more!",
    ],
    # New message categories
    "enqueue": [
        "Queued up **{title}**! Let's get this party started~",
        "Your track **{title}** is now in line~",
        "**{title}** has joined the party!",
    ],
    "secret_enqueue": [
        "Hehe~ **{title}** is a secret queue. Shh~",
        "Top secret track **{title}** has been tucked away~",
        "Your private tune **{title}** has been hidden till its time~",
    ],
    "queue_secret": [
        "🔒 Secret track at position {pos}. Patience!",
        "🤫 There's something secret at {pos}. Just wait!",
        "❔ Hidden song number {pos} — suspense!",
    ],
}

def get_response(category, **kwargs):
    return random.choice(RESPONSES[category]).format(**kwargs)


def format_duration(seconds):
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02}:{secs:02}"


def parse_lrc(lrc_text):
    parsed = []
    for line in lrc_text.splitlines():
        if line.startswith("["):
            parts = line.split("]")
            for part in parts[:-1]:
                ts = part.strip("[]")
                try:
                    m, s = map(float, ts.split(':'))
                    parsed.append((m * 60 + s, parts[-1]))
                except:
                    continue
    return parsed

async def fetch_lrc(query):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://lrclib.net/api/search?q={query}") as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if not data:
                return None
            track_id = data[0]['id']
            async with session.get(f"https://lrclib.net/api/get?track_id={track_id}") as lrc_resp:
                if lrc_resp.status != 200:
                    return None
                j = await lrc_resp.json(content_type=None)
                return j.get("syncedLyrics")


def download_song(query):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(cache_dir, '%(id)s.%(ext)s'),
        'default_search': 'ytsearch',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'ffmpeg_location': './ffmpeg',
            'preferredcodec': 'opus',
            'preferredquality': '0',
        }]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        if 'entries' in info:
            info = info['entries'][0]
        filename = ydl.prepare_filename(info).rsplit('.', 1)[0] + '.opus'
        return filename

async def cache_song(query):
    if query in cache:
        return
    loop = asyncio.get_event_loop()
    try:
        path = await loop.run_in_executor(None, download_song, query)
        cache[query] = path
    except Exception as e:
        print(f"Caching failed for {query}: {e}")


def get_youtube_info(query):
    if query in cache:
        ydl_opts = {'quiet': True, 'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
        return cache[query], info['title'], info.get('thumbnail'), int(info.get('duration', 0))
    ydl_opts = {
        'format': 'bestaudio[ext=webm][acodec=opus]/bestaudio',
        'quiet': True,
        'default_search': 'ytsearch',
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if 'entries' in info:
            info = info['entries'][0]
        return info['url'], info['title'], info.get('thumbnail'), int(info.get('duration', 0))

class ControlsView(discord.ui.View):
    def __init__(self, vc, guild_id, query):
        super().__init__(timeout=None)
        self.vc = vc
        self.guild_id = guild_id
        self.current_query = query
        self.start_time = time.time()
        self.paused_time = 0
        self.last_skip_time = 0
        self.last_prev_time = 0
        self.throttle_interval = 3.0

    @discord.ui.button(label='⏮ Previous', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.time()
        if now - self.last_prev_time < self.throttle_interval:
            return await interaction.response.send_message(f'Please wait {int(self.throttle_interval)}s before going back again.', ephemeral=True)
        self.last_prev_time = now
        hist = play_history.get(self.guild_id, deque())
        if not hist:
            return await interaction.response.send_message('No previous track in history.', ephemeral=True)
        prev_query = hist.pop()
        song_queues[self.guild_id].appendleft((self.current_query, interaction, False))
        self.vc.stop()
        await interaction.response.defer()

    @discord.ui.button(label='⏯ Pause/Resume', style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.vc.is_playing():
            self.vc.pause()
            self.paused_time = time.time() - self.start_time
        elif self.vc.is_paused():
            self.vc.resume()
            self.start_time = time.time() - self.paused_time
        await interaction.response.defer()

    @discord.ui.button(label='⏭ Next', style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.time()
        if now - self.last_skip_time < self.throttle_interval:
            return await interaction.response.send_message(f'Please wait {int(self.throttle_interval)}s before skipping again.', ephemeral=True)
        self.last_skip_time = now
        self.vc.stop()
        await interaction.response.defer()

async def send_now_playing(interaction, title, thumb, duration, lrc_data):
    embed = discord.Embed(title="Now Playing", description=f"**{title}**", color=0xff99cc)
    if thumb:
        embed.set_thumbnail(url=thumb)
    embed.add_field(name="Progress", value=f"`00:00 / {format_duration(duration)}`", inline=False)
    prev_line = lrc_data[0][1] if lrc_data else ""
    curr_line = lrc_data[0][1] if lrc_data else ""
    next_line = lrc_data[1][1] if len(lrc_data) > 1 else ""
    embed.add_field(name="Lyrics", value=f"{prev_line}\n**{curr_line}**\n{next_line}", inline=False)

    vc = interaction.guild.voice_client
    view = ControlsView(vc, interaction.guild.id, title)
    msg = await interaction.followup.send(embed=embed, view=view)

    q = song_queues.get(interaction.guild.id, deque())
    if q:
        next_q = q[0][0]
        bot.loop.create_task(cache_song(next_q))

    idx = 0
    while vc and (vc.is_playing() or vc.is_paused()):
        elapsed = view.paused_time if vc.is_paused() else time.time() - view.start_time
        embed.set_field_at(0, name="Progress", value=f"`{format_duration(elapsed)} / {format_duration(duration)}`", inline=False)
        if lrc_data:
            for i, (ts, _) in enumerate(lrc_data):
                if elapsed >= ts:
                    idx = i
                else:
                    break
            prev_line = lrc_data[idx-1][1] if idx > 0 else ""
            curr_line = lrc_data[idx][1]
            next_line = lrc_data[idx+1][1] if idx+1 < len(lrc_data) else ""
            embed.set_field_at(1, name="Lyrics", value=f"{prev_line}\n**{curr_line}**\n{next_line}", inline=False)
        try:
            await msg.edit(embed=embed, view=view)
        except:
            pass
        await asyncio.sleep(1)

@bot.tree.command(name="play", description="Play a song")
@app_commands.describe(query="Search or link")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("Join a voice channel first!")

    gid = interaction.guild.id
    song_queues.setdefault(gid, deque())
    was_empty = not song_queues[gid]
    song_queues[gid].append((query, interaction, False))

    play_history.setdefault(gid, deque())

    if was_empty:
        bot.loop.create_task(play_next(gid))
    else:
        await interaction.followup.send(get_response("play", title=query))

async def play_next(guild_id):
    if guild_id not in song_queues or not song_queues[guild_id]:
        return

    query, interaction, _ = song_queues[guild_id].popleft()
    try:
        vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect()
        play_history[guild_id].append(query)

        url, title, thumb, duration = get_youtube_info(query)
        source = await discord.FFmpegOpusAudio.from_probe(
            url,
            method='fallback',
            executable='./ffmpeg',
            before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            options='-b:a 256k'
        )
        vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop))

        await interaction.followup.send(get_response("play", title=title))
        raw_lrc = await fetch_lrc(title)
        lrc_data = parse_lrc(raw_lrc) if raw_lrc else []
        bot.loop.create_task(send_now_playing(interaction, title, thumb, duration, lrc_data))

    except Exception as e:
        print(f"Error in play_next: {e}")
        await interaction.followup.send("Failed to play the song.")
        await play_next(guild_id)

@bot.tree.command(name="enqueue", description="Add a song to the queue")
@app_commands.describe(query="Search or link", secret="Queue privately (secret)")
async def enqueue(interaction: discord.Interaction, query: str, secret: bool = False):
    gid = interaction.guild.id
    song_queues.setdefault(gid, deque())
    song_queues[gid].append((query, interaction, secret))
    if secret:
        await interaction.response.send_message(get_response("secret_enqueue", title=query), ephemeral=True)
    else:
        await interaction.response.send_message(get_response("enqueue", title=query))

@bot.tree.command(name="queue", description="Show the song queue")
async def queue_list(interaction: discord.Interaction):
    gid = interaction.guild.id
    q = song_queues.get(gid, deque())
    embed = discord.Embed(title="Up Next!", color=0x00ffcc)
    if not q:
        embed.description = "No songs in queue~"
    else:
        desc_lines = []
        for idx, (item, _, secret) in enumerate(q, start=1):
            if secret:
                desc_lines.append(f"**{idx}.** {get_response('queue_secret', pos=idx)}")
            else:
                desc_lines.append(f"**{idx}.** {item}")
        embed.description = "\n".join(desc_lines)
    await interaction.response.send_message(embed=embed)

# other commands unchanged...

@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message(get_response("pause"))

@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message(get_response("resume"))

@bot.tree.command(name="clear", description="Clear the queue")
async def clear(interaction: discord.Interaction):
    song_queues[interaction.guild.id] = deque()
    await interaction.response.send_message(get_response("enqueue", title="Queue cleared~"))

@bot.tree.command(name="stop", description="Stop current song")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message(get_response("skip"))
    else:
        await interaction.response.send_message("Nothing is playing right now~")

@bot.tree.command(name="end", description="Stop music, clear queue, and leave voice channel")
async def end(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    song_queues[interaction.guild.id] = deque()
    if vc:
        await vc.disconnect()
    await interaction.response.send_message(get_response("end"))

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Himari is ready~ as {bot.user}")

bot.run(DISCORD_TOKEN)
