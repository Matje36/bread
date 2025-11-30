import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import asyncio
import time
import io
from openai import OpenAI
from dotenv import load_dotenv
import qrcode
from io import BytesIO
from datetime import timedelta
import re
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
import random
import asyncpg
from typing import Optional, Dict, Any

# temp
GUILD_ID = 1324115004735230082

load_dotenv()
# read secrets from environment (set these in Railway project settings)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # optional; if set uses Postgres for persistence
if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN environment variable is required")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY environment variable is required")

# basics of the script
class BotClient(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
    
    # console startup
    async def on_ready(self):
        print(f"Logged in as {self.user}")

        # guild-specific sync only (no global sync)
        try:
            guild = discord.Object(id=GUILD_ID)
            synced = await self.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {guild.id}")
        except Exception as e:
            print(f"Error syncing commands to guild: {e}")

        # DEBUG: list app-commands present in the tree (helps verify guild-scoped registration)
        try:
            cmds = [c.name for c in self.tree.walk_commands()]
            print("App-commands in tree:", cmds)
        except Exception as e:
            print("Failed to list commands:", e)

bot = BotClient()

# Voeg een globale TIMEOUT_CHANNEL_ID toe (kan je aanpassen)
TIMEOUT_CHANNEL_ID = 1434970727647281182 

# Utility: parse duration strings like "5m", "1h", "1d", "1h30m"
_duration_regex = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", re.I)
def parse_duration(text: str) -> timedelta | None:
    """
    Parse simple duration strings like '1d', '2h', '30m', '1h30m', '1d 2h 5m 10s'.
    Returns a timedelta or None if invalid.
    """
    if not text or not text.strip():
        return None
    text = text.replace(",", " ").strip().lower()
    m = _duration_regex.fullmatch(text)
    if not m:
        return None
    days = int(m.group(1)) if m.group(1) else 0
    hours = int(m.group(2)) if m.group(2) else 0
    minutes = int(m.group(3)) if m.group(3) else 0
    seconds = int(m.group(4)) if m.group(4) else 0
    if days == 0 and hours == 0 and minutes == 0 and seconds == 0:
        return None
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

# Timeout Modal
class TimeoutModal(discord.ui.Modal):
    def __init__(self, target_user: discord.Member):
        super().__init__(title=f"Time-Out {target_user.display_name}")
        self.target_user = target_user
        self.add_item(discord.ui.TextInput(label="How long is the time-out?", placeholder="5m, 1h, 1d"))
        self.add_item(discord.ui.TextInput(label="Reason?", placeholder="Reason for time-out", style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction: discord.Interaction):
        # permission check: user invoking must have moderate_members permission
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ You don't have permission to timeout members.", ephemeral=True)
            return
        duration_text = self.children[0].value.strip()
        reason = self.children[1].value.strip() or "No reason provided"
        delta = parse_duration(duration_text)
        if delta is None:
            await interaction.response.send_message("❌ Invalid duration. Example formats: `5m`, `1h`, `1d`, `1h30m`.", ephemeral=True)
            return
        try:
            # Member.edit(timeout=...) expects a datetime (UTC) or None
            until = datetime.utcnow() + delta
            await self.target_user.edit(timeout=until, reason=f"Timed out by {interaction.user} — {reason}")
            await interaction.response.send_message(f"⏰ Time-out applied to {self.target_user.mention} for `{duration_text}`.\nReason: {reason}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to timeout that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error applying timeout: {e}", ephemeral=True)

# Kick Modal
class KickModal(discord.ui.Modal):
    def __init__(self, target_user: discord.Member):
        super().__init__(title=f"Kick {target_user.display_name}")
        self.target_user = target_user
        self.add_item(discord.ui.TextInput(label="Reason?", placeholder="Reason for kick", style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ You don't have permission to kick members.", ephemeral=True)
            return
        reason = self.children[0].value.strip() or "No reason provided"
        try:
            await self.target_user.kick(reason=f"Kicked by {interaction.user} — {reason}")
            await interaction.response.send_message(f"👢 {self.target_user.mention} has been kicked.\nReason: {reason}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error kicking user: {e}", ephemeral=True)

# Ban Modal
class BanModal(discord.ui.Modal):
    def __init__(self, target_user: discord.Member):
        super().__init__(title=f"Ban {target_user.display_name}")
        self.target_user = target_user
        self.add_item(discord.ui.TextInput(label="Reason?", placeholder="Reason for ban", style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ You don't have permission to ban members.", ephemeral=True)
            return
        reason = self.children[0].value.strip() or "No reason provided"
        try:
            await self.target_user.ban(reason=f"Banned by {interaction.user} — {reason}")
            await interaction.response.send_message(f"🔨 {self.target_user.mention} has been banned.\nReason: {reason}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error banning user: {e}", ephemeral=True)

# settings user CALL buttons
class Call(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user
        self.muted = False
        self.deafened = False

    @discord.ui.button(label="Move to TIME-OUT", style=discord.ButtonStyle.red, emoji="😥")
    async def move(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Permission: move members requires manage_roles? Actually bot needs move_members permission (connect/move)
        channel = interaction.guild.get_channel(TIMEOUT_CHANNEL_ID)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("⚠️ TIME-OUT channel not found or invalid!", ephemeral=True)
            return

        if not self.target_user.voice or not self.target_user.voice.channel:
            await interaction.response.send_message(f"{self.target_user.mention} is not in a voice channel!", ephemeral=True)
            return

        try:
            await self.target_user.move_to(channel)
            await interaction.response.send_message(f"✅ {self.target_user.mention} has been moved to the TIME-OUT channel!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to move that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Mute", style=discord.ButtonStyle.blurple, emoji="📢")
    async def mute_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target_user.voice:
            await interaction.response.send_message(f"{self.target_user.mention} is not in a voice channel!", ephemeral=True)
            return
        try:
            self.muted = not self.muted
            await self.target_user.edit(mute=self.muted)
            button.label = f"{'Unmute' if self.muted else 'Mute'}"
            await interaction.response.edit_message(view=self)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to mute/unmute that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Deafen", style=discord.ButtonStyle.red, emoji="🙉")
    async def deafen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target_user.voice:
            await interaction.response.send_message(f"{self.target_user.mention} is not in a voice channel!", ephemeral=True)
            return
        try:
            self.deafened = not self.deafened
            await self.target_user.edit(deafen=self.deafened)
            button.label = f"{'Undeafen' if self.deafened else 'Deafen'}"
            await interaction.response.edit_message(view=self)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to deafen that user!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

# Dropdown voor rollen selecteren
class RoleDropdown(discord.ui.Select):
    def __init__(self, target_user: discord.Member, guild_roles: list):
        self.target_user = target_user
        self.guild_roles = guild_roles
        
        # Maak opties van alle rollen (behalve @everyone)
        options = []
        for role in guild_roles:
            if role.name != "@everyone" and not role.managed:  # Filter @everyone en bot roles
                options.append(discord.SelectOption(
                    label=role.name,
                    value=str(role.id),
                    description=f"Give {role.name} role to user"
                ))
        
        # Beperk tot 25 opties (Discord limiet)
        options = options[:25]
        
        super().__init__(
            placeholder="Select a role to give...",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        role = interaction.guild.get_role(role_id)
        
        if not role:
            await interaction.response.send_message("❌ Role not found!", ephemeral=True)
            return
        
        try:
            await self.target_user.add_roles(role)
            await interaction.response.send_message(f"✅ {role.mention} role given to {self.target_user.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to assign this role!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

# settings user ROLES buttons
class ROLES(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user
        
        # Voeg dropdown toe met alle server rollen
        guild_roles = target_user.guild.roles
        self.add_item(RoleDropdown(target_user, guild_roles))
    
    @discord.ui.button(label="Give Admin", style=discord.ButtonStyle.red, emoji="😎")
    async def a_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Zoek de admin rol
        admin_role = discord.utils.get(interaction.guild.roles, name="admin")
        if not admin_role:
            await interaction.response.send_message("❌ Admin role not found!", ephemeral=True)
            return
        
        try:
            await self.target_user.add_roles(admin_role)
            await interaction.response.send_message(f"✅ Admin role given to {self.target_user.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to assign roles!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Take All Roles", style=discord.ButtonStyle.blurple, emoji="😐")
    async def b_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Verwijder alle rollen behalve @everyone
            roles_to_remove = [role for role in self.target_user.roles if role != interaction.guild.default_role]
            if roles_to_remove:
                await self.target_user.remove_roles(*roles_to_remove)
            await interaction.response.send_message(f"✅ All roles removed from {self.target_user.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to remove roles!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

# SETTINGS USER SERVER
class SERVER(discord.ui.View):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user

    @discord.ui.button(label="Time-out", style=discord.ButtonStyle.green, emoji="⏰")
    async def a_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutModal(self.target_user))

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.red, emoji="👢")
    async def b_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KickModal(self.target_user))

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.blurple, emoji="🔨")
    async def c_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BanModal(self.target_user))

# user setting drop menu
class Menu(discord.ui.Select):
    def __init__(self, user: discord.Member):
        self.target_user = user
        options = [
            # disabled option so it can't be selected
            discord.SelectOption(
                label=f"──── @{self.target_user.display_name} Settings ────",
                value="noop",
                description="Header / choose an action below",
                default=False
            ),
            discord.SelectOption(
                label="CALL",
                value="CALL",
                description="Control the call of the user!",
                emoji="☎️"
            ),
            discord.SelectOption(
                label="ROLES",
                value="ROLES",
                description="Add roles to the user or take them!",
                emoji="😍"
            ),
            discord.SelectOption(
                label="SERVER",
                value="SERVER",
                description="DANGERZONE!!",
                emoji="🤣"
            )
        ]
        # Note: Discord SelectOption doesn't have 'disabled' in many bindings; we'll treat "noop" specially in callback.
        super().__init__(placeholder="CLICK AN OPTION PLS!", min_values=1, max_values=1, options=options)
    
    # label name voor if
    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "noop":
            await interaction.response.send_message("**DONT SELECT THIS ONE!**\n\nSelect a different option!", ephemeral=True)
            return
        elif self.values[0] == "CALL":
            await interaction.response.send_message(view=Call(self.target_user), ephemeral=True)
        elif self.values[0] == "ROLES":
            await interaction.response.send_message(view=ROLES(self.target_user), ephemeral=True)
        elif self.values[0] == "SERVER":
            await interaction.response.send_message(view=SERVER(self.target_user), ephemeral=True)

class MenuView(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__()
        self.add_item(Menu(user))

@bot.tree.command(
    name="settings",
    description="User settings!",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(user="The user whose settings you want to view")
async def settings(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(view=MenuView(user), ephemeral=True)

# speak in een text channel
@bot.tree.command(
    name="speak",
    description="Send a message to a specific channel!",
    guild=discord.Object(id=GUILD_ID)
)
async def speak(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await channel.send(message)
    await interaction.response.send_message(f"✅ Sent your message to {channel.mention}!", ephemeral=True)

# -------------------------------------------------
# Poll system (simplified & reliable)
# -------------------------------------------------

# JSON file path
POLL_FILE = os.path.join(os.path.dirname(__file__), "storage", "poll.json")

# Emoji icons for up to 5 options
_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

# Ensure storage dir exists
os.makedirs(os.path.dirname(POLL_FILE), exist_ok=True)

# -------------------------------------------------
# Utility functions
# -------------------------------------------------
def load_polls():
    """Load all polls from JSON file."""
    if os.path.exists(POLL_FILE):
        with open(POLL_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_polls(polls):
    """Save all polls to JSON file."""
    os.makedirs(os.path.dirname(POLL_FILE), exist_ok=True)
    with open(POLL_FILE, "w", encoding="utf-8") as f:
        json.dump(polls, f, indent=4, ensure_ascii=False)

def _build_poll_embed(poll_name: str, poll_data: dict, closed: bool = False) -> discord.Embed:
    """Create a clean, professional poll embed."""
    title = f"📊 {poll_data['question']}"
    color = discord.Color.red() if closed else discord.Color.blurple()
    embed = discord.Embed(title=title, color=color)

    options = poll_data["options"]
    votes = poll_data.get("votes", {})
    counts = [0] * len(options)
    for uid, idx in votes.items():
        try:
            idx_int = int(idx)
        except:
            # in case idx is already int, int() will still work; this is safe
            try:
                idx_int = idx
            except:
                continue
        if 0 <= idx_int < len(options):
            counts[idx_int] += 1

    # Add description with votes and percentages
    total_votes = sum(counts)
    lines = []
    for i, opt in enumerate(options):
        percent = (counts[i] / total_votes * 100) if total_votes > 0 else 0
        bar = "▰" * int(percent // 10) + "▱" * (10 - int(percent // 10))
        lines.append(f"{_EMOJIS[i]} **{opt}**\n{bar} `{counts[i]} votes`  ({percent:.1f}%)")
    embed.description = "\n\n".join(lines)

    # Show voters if poll is not anonymous
    if not poll_data.get("anonymous", True) and not closed:
        voters_per_opt = [[] for _ in options]
        for uid, idx in votes.items():
            try:
                idx_int = int(idx)
            except:
                continue
            if 0 <= idx_int < len(options):
                voters_per_opt[idx_int].append(f"<@{uid}>")
        for i, voters in enumerate(voters_per_opt):
            voter_text = ", ".join(voters) if voters else "— no votes —"
            embed.add_field(name=f"{_EMOJIS[i]} {options[i]}", value=voter_text, inline=False)

    creator = poll_data.get("creator_name", "Unknown")
    footer_status = "❌ Closed" if closed else "✅ Open"
    embed.set_footer(text=f"Poll: {poll_name} • Created by {creator} • {footer_status}")
    return embed

# -------------------------------------------------
# Buttons for polls
# -------------------------------------------------
class PollButton(discord.ui.Button):
    def __init__(self, label: str, poll_name: str, index: int):
        # sanitize custom_id (no spaces)
        safe_name = re.sub(r"\s+", "_", poll_name)
        super().__init__(style=discord.ButtonStyle.secondary, label=label, custom_id=f"poll_button_{safe_name}_{index}")
        self.index = index
        self.poll_name = poll_name

    async def callback(self, interaction: discord.Interaction):
        polls = load_polls()
        poll_data = polls.get(self.poll_name)
        if not poll_data:
            await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
            return
        if poll_data.get("closed", False):
            await interaction.response.send_message("This poll is already closed.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        previous = poll_data["votes"].get(user_id)
        # store indices as integers for easier logic
        if previous is not None:
            try:
                previous_int = int(previous)
            except:
                previous_int = previous
        else:
            previous_int = None

        if previous_int == self.index:
            # remove vote
            del poll_data["votes"][user_id]
            text = "Your vote has been removed."
        else:
            poll_data["votes"][user_id] = self.index
            text = f"You voted for option {self.index + 1}."

        save_polls(polls)
        new_embed = _build_poll_embed(self.poll_name, poll_data)
        # try to edit the original poll message if possible
        try:
            await interaction.message.edit(embed=new_embed)
        except Exception:
            pass

        await interaction.response.send_message(text, ephemeral=True)

class PollView(discord.ui.View):
    def __init__(self, poll_name: str, poll_data: dict):
        super().__init__(timeout=None)
        self.poll_name = poll_name
        self.poll_data = poll_data
        for i, opt in enumerate(poll_data["options"]):
            label = opt if len(opt) <= 80 else f"Option {i+1}"
            self.add_item(PollButton(label, poll_name, i))

# -------------------------------------------------
# /poll command
# -------------------------------------------------
@bot.tree.command(
    name="poll",
    description="Create a poll with 2–5 options and optional timer.",
    guild=discord.Object(id=GUILD_ID)
)
async def poll(
    interaction: discord.Interaction,
    poll_name: str,
    question: str,
    option1: str,
    option2: str,
    option3: str = None,
    option4: str = None,
    option5: str = None,
    channel: discord.TextChannel | None = None,
    anonymously: bool = True,
    timer: int = 0  # seconds
):
    polls = load_polls()

    if poll_name in polls:
        await interaction.response.send_message("❌ A poll with that name already exists.", ephemeral=True)
        return

    options = [o for o in (option1, option2, option3, option4, option5) if o]
    if len(options) < 2:
        await interaction.response.send_message("❌ You must provide at least 2 options.", ephemeral=True)
        return
    if len(options) > 5:
        await interaction.response.send_message("❌ Maximum of 5 options allowed.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    creator = interaction.user

    poll_data = {
        "question": question,
        "options": options,
        "anonymous": anonymously,
        "message_id": None,
        "channel_id": target_channel.id,
        "creator_id": creator.id,
        "creator_name": creator.display_name,
        "votes": {},
        "closed": False
    }

    polls[poll_name] = poll_data
    save_polls(polls)

    embed = _build_poll_embed(poll_name, poll_data)
    view = PollView(poll_name, poll_data)

    poll_message = await target_channel.send(embed=embed, view=view)
    polls[poll_name]["message_id"] = poll_message.id
    save_polls(polls)

    anon_text = "anonymous" if anonymously else "non-anonymous"
    msg = f"✅ Poll **{poll_name}** created in {target_channel.mention} ({anon_text})"
    if timer > 0:
        msg += f" — will automatically close in {timer} seconds."
    await interaction.response.send_message(msg, ephemeral=True)

    # Auto-close after timer (run in background so command doesn't block)
    if timer > 0:
        async def _auto_close():
            await asyncio.sleep(timer)
            polls_local = load_polls()
            if poll_name in polls_local:
                polls_local[poll_name]["closed"] = True
                save_polls(polls_local)
                closed_embed = _build_poll_embed(poll_name, polls_local[poll_name], closed=True)
                try:
                    await poll_message.edit(embed=closed_embed, view=None)
                except Exception:
                    pass
                try:
                    await target_channel.send(f"🕒 Poll **{poll_name}** has automatically closed!")
                except Exception:
                    pass
        asyncio.create_task(_auto_close())


# /results command

@bot.tree.command(
    name="results",
    description="Show the results of a poll.",
    guild=discord.Object(id=GUILD_ID)
)
async def results(interaction: discord.Interaction, poll_name: str):
    polls = load_polls()
    poll_data = polls.get(poll_name)
    if not poll_data:
        await interaction.response.send_message("❌ Poll not found.", ephemeral=True)
        return

    embed = _build_poll_embed(poll_name, poll_data, closed=poll_data.get("closed", False))
    await interaction.response.send_message(embed=embed)

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# /ask command (AI integration)
@bot.tree.command(name="ask", description="Ask the AI a question", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    question="What do you want to ask?",
    anonymously="Whether to send anonymously (true/false)"
)
async def ask(interaction: discord.Interaction, question: str, anonymously: bool = False):
    await interaction.response.defer(thinking=True)

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant inside a Discord server."},
                {"role": "user", "content": question}
            ],
            max_tokens=800
        )
        # Depending on SDK response shape
        answer = ""
        if hasattr(response, "choices") and len(response.choices) > 0:
            # This handles typical shapes where choices[0].message.content exists
            try:
                answer = response.choices[0].message.content.strip()
            except Exception:
                # fallback to other potential shapes
                answer = str(response)
        else:
            answer = str(response)
    except Exception as e:
        # handle OpenAI or network errors gracefully
        await interaction.followup.send(f"❌ Error contacting AI: {e}", ephemeral=True)
        return

    if anonymously:
        embed = discord.Embed(
            title="💭 Anonymous Question",
            description=f"**Question:** {question}\n\n**Answer:** {answer}",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Sent anonymously 🕵️‍♂️")
    else:
        embed = discord.Embed(
            title=f"💬 Question from {interaction.user.display_name}",
            description=f"**Question:** {question}\n\n**Answer:** {answer}",
            color=discord.Color.green()
        )

    await interaction.followup.send(embed=embed)

# qr code section 
def generate_qr(url: str) -> BytesIO:
    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


@bot.tree.command(name="qr", description="Generate a QR code from a link",guild=discord.Object(id=GUILD_ID))
@app_commands.describe(url="The link you want to turn into a QR code")
async def qr(interaction: discord.Interaction, url: str):

    await interaction.response.defer(thinking=True)

    try:
        buffer = generate_qr(url)
        file = discord.File(buffer, filename="qr.png")

        await interaction.followup.send(
            content=f"This is ur QR code, link: `{url}`",
            file=file
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to generate QR code: `{e}`",
            ephemeral=True,
        )


#VCSTATS
STATS_FILE = os.path.join(os.path.dirname(__file__), "storage", "vcstats.json")

# --- Database (optional) for persistent vcstats on Railway ---
_db_pool: Optional[asyncpg.pool.Pool] = None

async def init_db():
    global _db_pool
    if not DATABASE_URL:
        _db_pool = None
        return
    _db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=3)
    async with _db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                data JSONB
            )
            """
        )

# file helpers (sync) used when DB is not configured
def _load_stats_file_sync() -> Dict[str, Any]:
    if not os.path.exists("storage"):
        os.makedirs("storage")
    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4)
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        data = {}
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return data

def _save_stats_file_sync(stats: dict):
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=4, ensure_ascii=False)

async def load_stats():
    """Async loader: prefer DB if configured, otherwise read file in executor."""
    if _db_pool is None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _load_stats_file_sync)
    async with _db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT data FROM kv_store WHERE key=$1", "vcstats")
        return row["data"] if row else {}

async def save_stats(stats: dict):
    """Async saver: prefer DB if configured, otherwise write file in executor."""
    if _db_pool is None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_stats_file_sync, stats)
        return
    async with _db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO kv_store(key, data) VALUES($1, $2) ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data",
            "vcstats",
            stats
        )

class VoiceTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_sessions = {}  # temp session tracking

#event lisent 

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        stats = await load_stats()

        user_id = str(member.id)
        if user_id not in stats:
            stats[user_id] = {
                "total_time": 0,
                "joins": 0,
                "avg_duration": 0,
                "total_sessions": 0,
                "joined_with": {},
                "mute_count": 0,
                "deaf_count": 0,
                "talk_time": 0
            }

        # ---------- JOINED VC ----------
        if before.channel is None and after.channel is not None:
            stats[user_id]["joins"] += 1
            self.user_sessions[user_id] = {
                "start": time.time(),
                "talk_start": None,
                "talk_time": 0,
                "channel": after.channel.id,
            }

            # Who they joined with
            for person in after.channel.members:
                if person.id != member.id:
                    pid = str(person.id)
                    stats[user_id]["joined_with"][pid] = stats[user_id]["joined_with"].get(pid, 0) + 1

        # ---------- LEFT VC ----------
        if before.channel is not None and after.channel is None:
            if user_id in self.user_sessions:
                session = self.user_sessions[user_id]
                duration = time.time() - session["start"]
                talk = session["talk_time"]

                stats[user_id]["total_time"] += duration
                stats[user_id]["talk_time"] += talk

                # avg duration
                stats[user_id]["total_sessions"] += 1
                total_ses = stats[user_id]["total_sessions"]
                stats[user_id]["avg_duration"] = stats[user_id]["total_time"] / total_ses

                del self.user_sessions[user_id]

        # ---------- MUTE / DEAF ----------
        if before.self_mute != after.self_mute and after.self_mute:
            stats[user_id]["mute_count"] += 1
        if before.self_deaf != after.self_deaf and after.self.deaf:
            stats[user_id]["deaf_count"] += 1

        await save_stats(stats)

    #  TRACK TALKING TIME

    @commands.Cog.listener()
    async def on_speaking(self, member, speaking):
        """
        Speaking events require a Discord voice gateway.
        This uses a simplified custom event your bot can emit.
        """
        user_id = str(member.id)

        if user_id not in self.user_sessions:
            return

        session = self.user_sessions[user_id]

        if speaking:
            session["talk_start"] = time.time()
        else:
            if session["talk_start"]:
                session["talk_time"] += time.time() - session["talk_start"]
                session["talk_start"] = None

    #  IMAGE GENERATOR (GTA STYLE)

    async def generate_gta_card(self, user: discord.User, data):

        WIDTH = 700
        HEIGHT = 1100  # portrait card
        card = Image.new("RGB", (WIDTH, HEIGHT), (15, 15, 15))
        draw = ImageDraw.Draw(card)

        # fonts (load with fallback)
        try:
            title_font = ImageFont.truetype("arial.ttf", 60)
            stat_font = ImageFont.truetype("arial.ttf", 38)
            small_font = ImageFont.truetype("arial.ttf", 30)
        except Exception:
            title_font = ImageFont.load_default()
            stat_font = ImageFont.load_default()
            small_font = ImageFont.load_default()

        # GTA red bar at top
        draw.rectangle([0, 0, WIDTH, 130], fill=(200, 0, 0))
        draw.text((40, 30), "VOICE STATS", font=title_font, fill="white")

        # Avatar (await the asset read and wrap bytes in BytesIO)
        avatar_bytes = await user.display_avatar.read()
        avatar = Image.open(BytesIO(avatar_bytes)).resize((350, 350))
        card.paste(avatar, (175, 150))

        # Draw username
        draw.text((30, 520), user.name.upper(), font=title_font, fill="white")

        # Stats section
        y = 650
        spacing = 80

        def stat(label, value):
            nonlocal y
            draw.text((40, y), f"{label}: {value}", font=stat_font, fill="white")
            y += spacing

        # format times
        def fmt(seconds):
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}h {m}m"

        stat("Total VC Time", fmt(data["total_time"]))
        stat("Times Joined", data["joins"])
        stat("Avg Duration", fmt(data["avg_duration"]))
        stat("Mute Count", data["mute_count"])
        stat("Deaf Count", data["deaf_count"])
        stat("Talking Time", fmt(data["talk_time"]))
        stat("Unique VC Partners", len(data["joined_with"]))

        # Save
        buffer = io.BytesIO()
        card.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    # ------------------------------------------------------
    #  /vcstats COMMAND
    # ------------------------------------------------------

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="vcstats", description="Show a user's voice activity stats.")
    async def vcstats(self, interaction: discord.Interaction, user: discord.User = None):
        await interaction.response.defer()
        try:
            if user is None:
                user = interaction.user

            stats = await load_stats()
            uid = str(user.id)

            if uid not in stats:
                await interaction.followup.send("User has no voice activity recorded.", ephemeral=True)
                return

            buffer = await self.generate_gta_card(user, stats[uid])
            file = discord.File(buffer, filename="vcstats.png")
            await interaction.followup.send(file=file)
        except Exception as e:
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ Error generating card: {e}", ephemeral=True)

    # ------------------------------------------------------
    #  /vctop COMMAND
    # ------------------------------------------------------

    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.command(name="vctop", description="Leaderboard of top VC users.")
    async def vctop(self, interaction: discord.Interaction):
        await interaction.response.defer()

        stats = await load_stats()

        sorted_users = sorted(stats.items(), key=lambda x: x[1]["total_time"], reverse=True)
        top = sorted_users[:10]

        desc = ""
        place = 1

        def fmt(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            return f"{h}h {m}m"

        for uid, data in top:
            user = await self.bot.fetch_user(int(uid))
            desc += f"**#{place}** — {user.name} — `{fmt(data['total_time'])}`\n"
            place += 1

        embed = discord.Embed(
            title="🏆 Voice Activity Leaderboard",
            description=desc,
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(VoiceTracker(bot))

#coinfliop command
@bot.tree.command(
    name="coinflip",
    description="Flip a coin!",
    guild=discord.Object(id=GUILD_ID)
)
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    await interaction.response.send_message(f"🪙 The coin landed on **{result}**!")


#random operator r6s
@bot.tree.command(
    name="roperator",
    description="Get a random operator from rainbow six siege!",
    guild=discord.Object(id=GUILD_ID)
)
@app_commands.describe(
    side="Choose Attack, Defense, or Random"
)
@app_commands.choices(side=[
    app_commands.Choice(name="Attack", value="attack"),
    app_commands.Choice(name="Defense", value="defense"),
    app_commands.Choice(name="Random", value="random"),
])
async def roperator(interaction: discord.Interaction, side: str | None = None):
    await interaction.response.defer()

    attackers = [
        "Ace", "Amaru", "Ash", "Blackbeard", "Blitz",
        "Brava", "Buck", "Capitão", "Deimos", "Dokkaebi",
        "Finka", "Flores", "Fuze", "Glaz", "Gridlock",
        "Grim", "Hibana", "Iana", "IQ", "Jackal",
        "Kali", "Lion", "Maverick", "Montagne", "Nøkk",
        "Nomad", "Osa", "Ram", "Rauora", "Sens",
        "Sledge", "Striker", "Thatcher", "Thermite", "Twitch",
        "Ying", "Zero", "Zofia"
    ]

    defenders = [
        "Alibi", "Aruni", "Castle", "Caveira", "Clash",
        "Ela", "Echo", "Frost", "Goyo", "Jäger",
        "Kaid", "Melusi", "Mute", "Oryx", "Pulse",
        "Ramattra", "Smoke", "Thunderbird", "Tachanka", "Valkyrie",
        "Wamai", "Warden", "Vigil", "Maestro", "Mozzie",
        "Thorn", "Lesion", "Doc", "Rook"
    ]

    # If no selection provided, treat as random
    choice = (side or "random").lower()

    if choice == "attack":
        operator = random.choice(attackers)
        side_name = "Attacker"
    elif choice == "defense":
        operator = random.choice(defenders)
        side_name = "Defender"
    elif choice == "random":
        side_name = random.choice(["Attacker", "Defender"])
        operator = random.choice(attackers if side_name == "Attacker" else defenders)
    else:
        # fallback: unknown value -> random
        side_name = random.choice(["Attacker", "Defender"])
        operator = random.choice(attackers if side_name == "Attacker" else defenders)

    await interaction.followup.send(f"🎯 You got **{operator}**! ({side_name}) have fun.")

#tick tack toe command
active_games = {}  # key: channel id, value: game state


class TicTacToe(discord.ui.View):
    def __init__(self, player1, player2):
        super().__init__(timeout=None)
        self.board = [['' for _ in range(3)] for _ in range(3)]
        self.players = [player1, player2]
        self.turn = 0  # index of current player
        self.game_over = False

    def check_winner(self):
        b = self.board
        # Rows, Columns, Diagonals
        lines = b + [list(col) for col in zip(*b)] + [[b[i][i] for i in range(3)], [b[i][2-i] for i in range(3)]]
        for line in lines:
            if line[0] != '' and all(x == line[0] for x in line):
                return line[0]
        if all(all(cell != '' for cell in row) for row in b):
            return 'Draw'
        return None

    async def make_bot_move(self):
        empty_cells = [(r, c) for r in range(3) for c in range(3) if self.board[r][c] == '']
        if empty_cells:
            r, c = random.choice(empty_cells)
            self.board[r][c] = 'O'
            button = self.children[r*3+c]
            button.label = 'O'
            button.disabled = True

    async def end_game(self, interaction, winner):
        self.game_over = True
        for child in self.children:
            child.disabled = True
        if winner == 'Draw':
            await interaction.response.edit_message(content="It's a draw!", view=self)
        else:
            winner_name = self.players[self.turn].mention if self.players[self.turn] != 'BOT' else 'BOT'
            await interaction.response.edit_message(content=f"{winner_name} wins!", view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.game_over:
            await interaction.response.send_message("Game over!", ephemeral=True)
            return False
        if interaction.user != self.players[self.turn] and self.players[self.turn] != 'BOT':
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return False
        return True


class TicTacToeButton(discord.ui.Button):
    def __init__(self, row, col):
        super().__init__(style=discord.ButtonStyle.secondary, label=' ', row=row)
        self.row = row
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToe = self.view
        if view.game_over:
            await interaction.response.send_message("Game is over!", ephemeral=True)
            return

        player_symbol = 'X'
        view.board[self.row][self.col] = player_symbol
        self.label = player_symbol
        self.disabled = True

        winner = view.check_winner()
        if winner:
            await view.end_game(interaction, winner)
            return

        view.turn = 1 - view.turn  # Switch turn

        if view.players[view.turn] == 'BOT':
            await view.make_bot_move()
            winner = view.check_winner()
            if winner:
                await view.end_game(interaction, winner)
                return
            view.turn = 0

        await interaction.response.edit_message(view=view)


# Create the 3x3 buttons
def create_board_view(player1, player2):
    view = TicTacToe(player1, player2)
    for r in range(3):
        for c in range(3):
            view.add_item(TicTacToeButton(r, c))
    return view


# Slash command
@bot.tree.command(name="oxo", description="Play Tic-Tac-Toe!", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="Mention a user to play against")
async def oxo(interaction: discord.Interaction, opponent: discord.User = None):
    if opponent and opponent != interaction.user:
        view = create_board_view(interaction.user, opponent)
        await interaction.response.send_message(f"{interaction.user.mention} vs {opponent.mention}", view=view)
    else:
        # Buttons for joining or playing against bot
        class ChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="Join as Player", style=discord.ButtonStyle.primary)
            async def join_player(self, button: discord.ui.Button, inter: discord.Interaction):
                # First two users to click
                if not hasattr(self, "players"):
                    self.players = [inter.user]
                    await inter.response.send_message(f"{inter.user.mention} joined! Waiting for second player...", ephemeral=True)
                    return
                elif len(self.players) == 1:
                    self.players.append(inter.user)
                    view = create_board_view(self.players[0], self.players[1])
                    await inter.response.edit_message(content=f"{self.players[0].mention} vs {self.players[1].mention}", view=view)
                    self.stop()
                else:
                    await inter.response.send_message("Already two players joined!", ephemeral=True)

            @discord.ui.button(label="Play Against Bot", style=discord.ButtonStyle.secondary)
            async def play_bot(self, button: discord.ui.Button, inter: discord.Interaction):
                view = create_board_view(inter.user, 'BOT')
                await inter.response.edit_message(content=f"{inter.user.mention} vs BOT", view=view)
                self.stop()

        await interaction.response.send_message("Choose how to play:", view=ChoiceView())

# Run the bot
@bot.event
async def on_ready():
    # Avoid re-syncing the command tree here — BotClient.on_ready already performs a guild sync.
    print(f"Logged in as {bot.user}")

# ensure the Cog is added and the bot is started in the same event loop
async def main():
    await init_db()
    await bot.add_cog(VoiceTracker(bot))
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
