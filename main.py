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
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageChops
from datetime import datetime
import random
import asyncpg
from typing import Optional, Dict, Any
import yt_dlp
from collections import deque
import traceback

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
        intents.voice_states = True  # Make sure this is here
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

# ---------------------------------------------------------
# STOPWATCH SYSTEM MET KNOPPEN + LIVE UPDATES
# ---------------------------------------------------------

class StopwatchView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=None)
        self.user = user
        self.running = False
        self.start_time = 0.0
        self.elapsed_saved = 0.0 # Opslag voor als we pauzeren
        self.task = None
        self.message = None

    def get_display_time(self):
        """Bereken de tijd om weer te geven."""
        if self.running:
            current_elapsed = time.time() - self.start_time
        else:
            current_elapsed = self.elapsed_saved
        
        return int(current_elapsed)

    async def update_loop(self):
        """Achtergrondtaak die de embed update."""
        try:
            while self.running:
                # We wachten 2 seconden om Discord Rate Limits te voorkomen!
                # Elke seconde updaten zorgt voor API bans/errors.
                await asyncio.sleep(2)
                
                if not self.running:
                    break

                seconds = self.get_display_time()
                h, m = divmod(seconds, 3600)
                m, s = divmod(m, 60)

                embed = discord.Embed(
                    title="⏱️ Stopwatch",
                    description=f"**`{h:02d}:{m:02d}:{s:02d}`**",
                    color=discord.Color.green()
                )

                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.NotFound:
                    self.running = False # Bericht bestaat niet meer, stop loop
                    break
                except Exception as e:
                    print(f"Stopwatch update error: {e}")
        except asyncio.CancelledError:
            pass # Taak is geannuleerd, dit is normaal

    @discord.ui.button(label="Start / Hervatten", style=discord.ButtonStyle.green)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("❌ Dit is niet jouw stopwatch.", ephemeral=True)

        if self.running:
            return await interaction.response.send_message("⏱️ Stopwatch draait al.", ephemeral=True)

        self.running = True
        # Als we hervatten, berekenen we de nieuwe starttijd o.b.v. wat we al hadden
        self.start_time = time.time() - self.elapsed_saved
        
        # Start de background update-task
        if self.task:
            self.task.cancel() # Zeker weten dat er geen oude tasks zijn
        self.task = asyncio.create_task(self.update_loop())

        await interaction.response.send_message("▶️ Stopwatch gestart!", ephemeral=True)

    @discord.ui.button(label="Pauze", style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("❌ Dit is niet jouw stopwatch.", ephemeral=True)

        if not self.running:
            return await interaction.response.send_message("❌ Stopwatch staat al stil.", ephemeral=True)

        # Stop de logica
        self.running = False
        if self.task:
            self.task.cancel()
        
        # Sla de tijd op
        self.elapsed_saved = time.time() - self.start_time

        # Update de embed direct naar de definitieve tijd
        seconds = int(self.elapsed_saved)
        h, m = divmod(seconds, 3600)
        m, s = divmod(m, 60)

        embed = discord.Embed(
            title="⏱️ Stopwatch (Gepauzeerd)",
            description=f"**`{h:02d}:{m:02d}:{s:02d}`**",
            color=discord.Color.orange()
        )

        await self.message.edit(embed=embed, view=self)
        await interaction.response.send_message("⏸️ Stopwatch gepauzeerd!", ephemeral=True)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.gray)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            return await interaction.response.send_message("❌ Dit is niet jouw stopwatch.", ephemeral=True)

        self.running = False
        if self.task:
            self.task.cancel()
        
        self.start_time = 0.0
        self.elapsed_saved = 0.0

        embed = discord.Embed(
            title="⏱️ Stopwatch",
            description="**`00:00:00`**",
            color=discord.Color.blurple()
        )

        await self.message.edit(embed=embed, view=self)
        await interaction.response.send_message("🔄 Stopwatch gereset!", ephemeral=True)


# ---------------------------------------------------------
# /stopwatch COMMAND
# ---------------------------------------------------------

@bot.tree.command(
    name="stopwatch",
    description="Start een stopwatch met live update en knoppen.",
    guild=discord.Object(id=GUILD_ID)
)
async def stopwatch(interaction: discord.Interaction):
    view = StopwatchView(interaction.user)

    embed = discord.Embed(
        title="⏱️ Stopwatch",
        description="**`00:00:00`**",
        color=discord.Color.blurple()
    )

    # We sturen het bericht en slaan het object op in de view
    # zodat de background loop het bericht kan editen.
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()



#coinfliop command
@bot.tree.command(
    name="coinflip",
    description="Flip a coin!",
    guild=discord.Object(id=GUILD_ID)
)
async def coinflip(interaction: discord.Interaction):
    result = random.choice(["Heads", "Tails"])
    await interaction.response.send_message(f"🪙 The coin landed on **{result}**!")



#tick tack toe command
active_games = {}  # key: channel id, value: game state


class TicTacToe(discord.ui.View):
    def __init__(self, player1, player2):
        super().__init__(timeout=None)
        self.board = [['' for _ in range(3)] for _ in range(3)]
        self.players = [player1, player2] # player1 is X, player2 is O (of 'BOT')
        self.turn = 0  # 0 voor player1, 1 voor player2
        self.game_over = False

    def check_winner(self):
        b = self.board
        # Check rows, columns, and diagonals
        lines = b + [list(col) for col in zip(*b)] + [[b[i][i] for i in range(3)], [b[i][2-i] for i in range(3)]]
        
        for line in lines:
            if line[0] != '' and all(x == line[0] for x in line):
                return line[0] # Geeft 'X' of 'O' terug
        
        if all(all(cell != '' for cell in row) for row in b):
            return 'Draw'
        
        return None

    def make_bot_move(self):
        if self.game_over: 
            return

        # Zoek alle lege plekken
        empty_cells = [(r, c) for r in range(3) for c in range(3) if self.board[r][c] == '']
        
        if empty_cells:
            # Kies willekeurige plek
            r, c = random.choice(empty_cells)
            self.board[r][c] = 'O'
            
            # Update de knop visueel
            # Omdat we buttons op volgorde toevoegen (0-8), kunnen we de index berekenen
            button_index = r * 3 + c
            button = self.children[button_index]
            button.label = 'O'
            button.style = discord.ButtonStyle.danger
            button.disabled = True

    async def end_game(self, interaction: discord.Interaction, winner: str):
        self.game_over = True
        
        # Schakel alle knoppen uit
        for child in self.children:
            child.disabled = True

        if winner == 'Draw':
            content = "🤝 It's a draw!"
        else:
            # Bepaal wie er heeft gewonnen
            winning_player = self.players[0] if winner == 'X' else self.players[1]
            winner_name = winning_player.mention if winning_player != 'BOT' else '🤖 The Bot'
            content = f"🏆 {winner_name} wins!"

        # Update het bericht om het einde te tonen
        await interaction.response.edit_message(content=content, view=self)

class TicTacToeButton(discord.ui.Button):
    def __init__(self, row, col):
        super().__init__(style=discord.ButtonStyle.secondary, label='\u200b', row=row)
        self.row = row
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        view: TicTacToe = self.view
        
        # 1. Validatie checks
        if view.game_over:
            await interaction.response.send_message("Game is over!", ephemeral=True)
            return

        current_player = view.players[view.turn]
        
        # Check of het de beurt is van de gebruiker die klikt
        if current_player == 'BOT':
            await interaction.response.send_message("Wait for the bot to move!", ephemeral=True)
            return
        
        if interaction.user != current_player:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        # 2. Speler zet (X of O)
        symbol = 'X' if view.turn == 0 else 'O'
        view.board[self.row][self.col] = symbol
        self.label = symbol
        self.style = discord.ButtonStyle.success if symbol == 'X' else discord.ButtonStyle.danger
        self.disabled = True

        # 3. Check winst na speler zet
        winner = view.check_winner()
        if winner:
            await view.end_game(interaction, winner)
            return

        # 4. Wissel beurt
        view.turn = 1 - view.turn
        next_player = view.players[view.turn]

        # 5. Als de volgende speler de BOT is, doe direct een zet
        if next_player == 'BOT':
            view.make_bot_move()
            
            # Check winst na bot zet
            winner = view.check_winner()
            if winner:
                await view.end_game(interaction, winner)
                return
            
            # Wissel beurt terug naar speler
            view.turn = 0

        # 6. Update het bord (als het spel nog niet voorbij is)
        current_name = view.players[view.turn].mention
        await interaction.response.edit_message(content=f"🎮 Tic-Tac-Toe: {current_name}'s turn", view=view)

# Helper functie om het bord te maken
def create_board_view(player1, player2):
    view = TicTacToe(player1, player2)
    for r in range(3):
        for c in range(3):
            view.add_item(TicTacToeButton(r, c))
    return view

# De command
@bot.tree.command(name="oxo", description="Play Tic-Tac-Toe!", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(opponent="Mention a user to play against")
async def oxo(interaction: discord.Interaction, opponent: discord.User = None):
    # Optie 1: Direct iemand uitgedaagd
    if opponent:
        if opponent.bot:
             await interaction.response.send_message("❌ You cannot challenge a discord bot directly, choose 'Play Against Bot'.", ephemeral=True)
             return
        if opponent == interaction.user:
             await interaction.response.send_message("❌ You cannot play against yourself!", ephemeral=True)
             return
             
        view = create_board_view(interaction.user, opponent)
        await interaction.response.send_message(f"🎮 **Tic-Tac-Toe**\n{interaction.user.mention} (X) vs {opponent.mention} (O)", view=view)
    
    # Optie 2: Menu weergeven
    else:
        class ChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.players = []

            @discord.ui.button(label="Join as Player", style=discord.ButtonStyle.primary, emoji="🙋‍♂️")
            async def join_player(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user in self.players:
                    await interaction.response.send_message("You already joined!", ephemeral=True)
                    return
                
                self.players.append(interaction.user)
                
                if len(self.players) == 1:
                    await interaction.response.send_message(f"✅ {interaction.user.mention} joined! Waiting for Player 2...", ephemeral=False)
                elif len(self.players) == 2:
                    # Start game pvp
                    view = create_board_view(self.players[0], self.players[1])
                    # We editten het originele bericht met het spelbord
                    await interaction.message.edit(
                        content=f"🎮 **Tic-Tac-Toe**\n{self.players[0].mention} (X) vs {self.players[1].mention} (O)", 
                        view=view
                    )
                    # We verwijderen dit 'joined' berichtje niet, of sturen een lege response om errors te voorkomen
                    # Bij een 'interaction' op een knop moet je reageren.
                    # Omdat we interaction.message.edit deden hierboven, doen we hier een silent defer of update
                    try:
                        await interaction.response.defer() 
                    except: 
                        pass
                    self.stop()

            @discord.ui.button(label="Play Against Bot", style=discord.ButtonStyle.secondary, emoji="🤖")
            async def play_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
                view = create_board_view(interaction.user, 'BOT')
                await interaction.response.edit_message(
                    content=f"🎮 **Tic-Tac-Toe**\n{interaction.user.mention} (X) vs 🤖 Bot (O)", 
                    view=view
                )
                self.stop()

        await interaction.response.send_message("⚔️ **Tic-Tac-Toe Setup**\nChoose how to play:", view=ChoiceView())


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

# settings command
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

#==============================================================================================================================================


# ---------------------------------------------------------
# MUSIC SYSTEM (English)
# ---------------------------------------------------------

# Options for FFmpeg to handle streaming stability
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# Options for yt-dlp to find the best audio format
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': 'True',
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0' # bind to ipv4
}

class MusicSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Dictionary to store queues: {guild_id: [song_info_dict, ...]}
        self.queues = {}
        # Dictionary to store the currently playing song info per guild
        self.current_song = {}

    def get_queue(self, guild_id):
        if guild_id not in self.queues:
            self.queues[guild_id] = []
        return self.queues[guild_id]

    async def play_next(self, interaction: discord.Interaction):
        """
        Recursive function to play the next song in the queue.
        """
        guild_id = interaction.guild_id
        queue = self.get_queue(guild_id)

        if not queue:
            # Queue is empty, clear current song
            self.current_song[guild_id] = None
            return

        # Get the next song (FIFO)
        song_info = queue.pop(0)
        self.current_song[guild_id] = song_info
        url = song_info['url']
        title = song_info['title']

        voice_client = interaction.guild.voice_client

        if not voice_client or not voice_client.is_connected():
            return

        # Define the callback for when the song ends
        def after_playing(error):
            if error:
                print(f"Error playing audio: {error}")
            # Schedule the next song safely in the event loop
            coro = self.play_next(interaction)
            fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"Error in after_playing: {e}")

        try:
            # Create the audio source (streaming directly from URL)
            source = discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS)
            # Make volume adjustable (standard 0.5 is usually good)
            source = discord.PCMVolumeTransformer(source, volume=0.5)
            
            voice_client.play(source, after=after_playing)
            
            # Use the stored channel to notify "Now Playing" (optional, to avoid spam)
            # await interaction.channel.send(f"🎶 Now playing: **{title}**")
        except Exception as e:
            print(f"Could not play song: {e}")
            await self.play_next(interaction)

    # ---------------------------------------------------------
    # COMMAND GROUP: /call (join, leave)
    # ---------------------------------------------------------
    call_group = app_commands.Group(name="call", description="Manage the bot in voice channels", guild_ids=[GUILD_ID])

    @call_group.command(name="join", description="Force the bot to join your voice channel.")
    async def call_join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ You are not in a voice channel.", ephemeral=True)
        
        channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        if voice_client:
            if voice_client.channel.id == channel.id:
                return await interaction.response.send_message("✅ I am already here!", ephemeral=True)
            await voice_client.move_to(channel)
        else:
            await channel.connect()
        
        await interaction.response.send_message(f"✅ Joined **{channel.name}**!", ephemeral=True)

    @call_group.command(name="leave", description="Force the bot to leave the voice channel.")
    async def call_leave(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client:
            # Clear queue when leaving
            if interaction.guild.id in self.queues:
                self.queues[interaction.guild.id].clear()
            
            await voice_client.disconnect()
            await interaction.response.send_message("👋 Left the voice channel.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ I am not in a voice channel.", ephemeral=True)

    # ---------------------------------------------------------
    # COMMAND GROUP: /sound (play, stop, queue)
    # ---------------------------------------------------------
    sound_group = app_commands.Group(name="sound", description="Music playback commands", guild_ids=[GUILD_ID])

    @sound_group.command(name="play", description="Play audio from a YouTube URL (adds to queue).")
    @app_commands.describe(url="YouTube link or search query")
    async def sound_play(self, interaction: discord.Interaction, url: str):
        # 1. Check if user is in VC
        if not interaction.user.voice:
            return await interaction.response.send_message("❌ You must be in a voice channel to play music.", ephemeral=True)

        await interaction.response.defer() # Processing might take a second

        # 2. Connect if not connected
        voice_client = interaction.guild.voice_client
        if not voice_client:
            try:
                voice_client = await interaction.user.voice.channel.connect()
            except Exception as e:
                return await interaction.followup.send(f"❌ Could not join channel: {e}")

        # 3. Extract Info using yt-dlp
        loop = asyncio.get_running_loop()
        try:
            with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                # Run extraction in executor to prevent blocking the bot
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                
                # If it's a playlist or search result, pick the first entry
                if 'entries' in info:
                    info = info['entries'][0]

                # Get the direct stream URL and title
                # 'url' in info usually points to the video file stream for yt-dlp
                stream_url = info['url'] 
                title = info['title']
                
                song = {'url': stream_url, 'title': title, 'requester': interaction.user.display_name}

        except Exception as e:
            return await interaction.followup.send(f"❌ Error fetching song info: {e}")

        # 4. Add to queue
        queue = self.get_queue(interaction.guild.id)
        queue.append(song)

        # 5. Provide feedback
        embed = discord.Embed(description=f"🎵 **Added to queue:** [{title}]({url})", color=discord.Color.blurple())
        await interaction.followup.send(embed=embed)

        # 6. If not playing, start playing
        if not voice_client.is_playing():
            await self.play_next(interaction)

    @sound_group.command(name="stop", description="Stop music and clear the queue.")
    async def sound_stop(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            # Clear queue
            if interaction.guild.id in self.queues:
                self.queues[interaction.guild.id].clear()
            
            voice_client.stop()
            await interaction.response.send_message("🛑 Music stopped and queue cleared.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)

    @sound_group.command(name="queue", description="Show the current music queue.")
    async def sound_queue(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        if not queue:
            return await interaction.response.send_message("📭 The queue is currently empty.", ephemeral=True)

        desc = ""
        for i, song in enumerate(queue[:10]): # Limit to 10 to avoid huge messages
            desc += f"**{i+1}.** {song['title']} (by {song['requester']})\n"
        
        if len(queue) > 10:
            desc += f"\n*...and {len(queue) - 10} more*"

        embed = discord.Embed(title="🎶 Current Queue", description=desc, color=discord.Color.green())
        await interaction.response.send_message(embed=embed)


#==============================================================================================================================================
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

def _load_stats_file_sync() -> Dict[str, Any]:
    os.makedirs("storage", exist_ok=True)
    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            print(f"DEBUG: Loaded stats from file: {data}")
            return data
    except (json.JSONDecodeError, ValueError):
        return {}

def _save_stats_file_sync(stats: dict):
    os.makedirs("storage", exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=4)
        f.flush()
    print(f"DEBUG: Saved stats to file: {stats}")

async def load_stats():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _load_stats_file_sync)

async def save_stats(stats: dict):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_stats_file_sync, stats)

# --- VOICE TRACKER COG ---
class VoiceTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_sessions = {}  # temp session tracking

    # Event listener
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        print(f"DEBUG: {member.name} | before: {before.channel} | after: {after.channel}")
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
        if before.self_deaf != after.self_deaf and after.self_deaf:
            stats[user_id]["deaf_count"] += 1

        await save_stats(stats)

    # ------------------------------------------------------------------
    #  IMAGE GENERATOR (IMPROVED LAYOUT)
    # ------------------------------------------------------------------
    async def generate_gta_card(self, user: discord.User, data):
        # Canvas Settings
        WIDTH, HEIGHT = 900, 1250
        # Modern Dark Grey background instead of solid black
        card = Image.new("RGBA", (WIDTH, HEIGHT), (25, 25, 30, 255)) 
        draw = ImageDraw.Draw(card)

        # --- Font Configuration (Simplified and Size Variables) ---
        FONT_PATH = "arial.ttf" 
        FALLBACK_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" # Linux fallback 1 (Bold)
        FALLBACK_FONT_PATH_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf" # Linux fallback 2 (Regular)

        # Text Size Variables (Eenvoudig aan te passen)
        SIZE_HEADER = 1200 
        SIZE_LABEL = 1000
        SIZE_VALUE = 1000

        def load_font(size, is_bold=False):
            """Laad een lettertype met fallbacks voor Linux/Railway."""
            if is_bold:
                paths = [
                    "arialbd.ttf", # Windows/Local Bold
                    FALLBACK_FONT_PATH,
                    FONT_PATH,
                    FALLBACK_FONT_PATH_REG
                ]
                print(f"DEBUG: Trying to load font size {size} bold={is_bold}")
            else:
                paths = [
                    FONT_PATH, # Windows/Local Regular
                    FALLBACK_FONT_PATH_REG,
                    FALLBACK_FONT_PATH,
                    "arialbd.ttf"
                ]
                print(f"DEBUG: Trying to load font size {size} bold={is_bold}")

            for p in paths:
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    continue
            
            # Laatste fallback
            return ImageFont.load_default()

        # Fonts
        font_header_bold = load_font(SIZE_HEADER, is_bold=True)
        font_label = load_font(SIZE_LABEL, is_bold=True)
        font_value = load_font(SIZE_VALUE, is_bold=False)
        
        # --- 1. BANNER & HEADER ---
        # Load avatar
        avatar_bytes = await user.display_avatar.read()
        avatar_src = Image.open(BytesIO(avatar_bytes)).convert("RGBA")

        # Create banner from avatar (Top 350px)
        banner_h = 350
        banner = avatar_src.copy().resize((WIDTH, banner_h))
        banner = banner.filter(ImageFilter.GaussianBlur(radius=2))
        
        # Gradient Fade for Banner (Top to Bottom fade out)
        # Create a gradient mask
        gradient = Image.new('L', (WIDTH, banner_h), 0)
        g_draw = ImageDraw.Draw(gradient)
        for y in range(banner_h):
            # Alpha goes from 255 (top) to 0 (bottom) for a fade-in effect on the *mask*, 
            # or 0 (top) to 255 (bottom) for a fade-out effect on the *overlay*.
            # We use an overlay, so we want the overlay to be full at the bottom.
            alpha = int(255 * (y / banner_h)) # Fade in overlay at bottom (Darkens the bottom)
            g_draw.line([(0, y), (WIDTH, y)], fill=alpha)
        
        # Apply darker overlay to banner
        overlay = Image.new("RGBA", (WIDTH, banner_h), (0, 0, 0, 150))
        banner = Image.alpha_composite(banner, overlay)
        
        # Paste banner onto card (no mask needed if we use alpha_composite with a darkened banner)
        card.paste(banner, (0, 0))

        # --- 2. AVATAR (Floating) ---
        av_size = 200
        av_x = (WIDTH - av_size) // 2
        av_y = banner_h - (av_size // 2) - 40
        
        # Circular Mask
        mask = Image.new("L", (av_size, av_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
        
        avatar_circle = avatar_src.resize((av_size, av_size))
        
        # Avatar Border (White/Red)
        border_sz = 6
        draw.ellipse(
            (av_x - border_sz, av_y - border_sz, av_x + av_size + border_sz, av_y + av_size + border_sz),
            fill=(200, 30, 30) # Red accent border
        )
        card.paste(avatar_circle, (av_x, av_y), mask)

        # --- 3. USERNAME ---
        name_text = user.name.upper()
        # Drop shadow for text
        # Gebruik draw.textlength in plaats van draw.textbbox voor nauwkeurigheid in sommige PIL versies
        t_w = draw.textlength(name_text, font=font_header_bold)
        name_x = (WIDTH - t_w) // 2
        name_y = av_y + av_size + 20
        
        # Zorg ervoor dat de schaduw niet te veel verschuift door alleen 3px te verschuiven
        draw.text((name_x + 3, name_y + 3), name_text, font=font_header_bold, fill=(0, 0, 0))
        draw.text((name_x, name_y), name_text, font=font_header_bold, fill=(255, 255, 255))

        # --- 4. STATS LIST ---
        # Helper to format time
        def fmt(seconds):
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}h {m}m"

        stats_list = [
            ("TOTAL TIME", fmt(data.get("total_time", 0))),
            ("TIMES JOINED", str(data.get("joins", 0))),
            ("AVG DURATION", fmt(data.get("avg_duration", 0))),
            ("MUTED", str(data.get("mute_count", 0))),
            ("DEAFENED", str(data.get("deaf_count", 0))),
            ("TALKING TIME", fmt(data.get("talk_time", 0))),
            ("FRIENDS MET", str(len(data.get("joined_with", {}))))
        ]

        start_y = name_y + 110
        item_height = 75
        margin_x = 50
        
        for i, (label, value) in enumerate(stats_list):
            y_pos = start_y + (i * (item_height + 15))
            
            # Row Background (Semi-transparent dark grey)
            # Draw on main card (no separate layer needed for solid color)
            draw.rectangle([margin_x, y_pos, WIDTH - margin_x, y_pos + item_height], fill=(40, 40, 45, 255))
            
            # Accent Bar on Left (Green for GTA Money vibe)
            draw.rectangle([margin_x, y_pos, margin_x + 8, y_pos + item_height], fill=(255, 0, 50))
            
            # Text
            # Label (Left)
            draw.text((margin_x + 30, y_pos + 12), label, font=font_label, fill=(220, 220, 220)) 
            
            # Value (Right)
            val_w = draw.textlength(value, font=font_value)
            draw.text((WIDTH - margin_x - val_w - 20, y_pos + 12), value, font=font_value, fill=(255, 255, 255))

        # Final Export
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
            
            print(f"DEBUG /vcstats: Looking for uid={uid}, available keys: {list(stats.keys())}")
            print(f"DEBUG /vcstats: Full stats data: {stats}")

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




# Run the bot

# ensure the Cog is added and the bot is started in the same event loop
async def main():
    await bot.add_cog(VoiceTracker(bot))
    await bot.add_cog(MusicSystem(bot))
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())