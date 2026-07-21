"""The /w "who is" command. Shows the stored profile for an in-game ID."""
import discord
from discord.ext import commands
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone
from .pimp_my_bot import theme
from .login_handler import LoginHandler

logger = logging.getLogger('alliance')


def _relative_age(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return f"<t:{int(dt.timestamp())}:R>"
    except (ValueError, OSError):
        return ""


def _format_big_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)

class WCommand(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = sqlite3.connect('db/changes.sqlite', timeout=30.0, check_same_thread=False)
        self.c = self.conn.cursor()

        self.level_mapping = {
            31: "30-1", 32: "30-2", 33: "30-3", 34: "30-4",
            35: "TC 1", 36: "TC 1-1", 37: "TC 1-2", 38: "TC 1-3", 39: "TC 1-4",
            40: "TC 2", 41: "TC 2-1", 42: "TC 2-2", 43: "TC 2-3", 44: "TC 2-4",
            45: "TC 3", 46: "TC 3-1", 47: "TC 3-2", 48: "TC 3-3", 49: "TC 3-4",
            50: "TC 4", 51: "TC 4-1", 52: "TC 4-2", 53: "TC 4-3", 54: "TC 4-4",
            55: "TC 5", 56: "TC 5-1", 57: "TC 5-2", 58: "TC 5-3", 59: "TC 5-4",
            60: "TC 6", 61: "TC 6-1", 62: "TC 6-2", 63: "TC 6-3", 64: "TC 6-4",
            65: "TC 7", 66: "TC 7-1", 67: "TC 7-2", 68: "TC 7-3", 69: "TC 7-4",
            70: "TC 8", 71: "TC 8-1", 72: "TC 8-2", 73: "TC 8-3", 74: "TC 8-4",
            75: "TC 9", 76: "TC 9-1", 77: "TC 9-2", 78: "TC 9-3", 79: "TC 9-4",
            80: "TC 10", 81: "TC 10-1", 82: "TC 10-2", 83: "TC 10-3", 84: "TC 10-4"
        }

    async def cog_unload(self):
        if hasattr(self, 'conn'):
            self.conn.close()

    @discord.app_commands.command(name='w', description='Fetches user info using ID.')
    @discord.app_commands.rename(fid='id')
    async def w(self, interaction: discord.Interaction, fid: str):
        await self.fetch_user_info(interaction, fid)

    @w.autocomplete('fid')
    async def autocomplete_fid(self, interaction: discord.Interaction, current: str):
        try:
            def _read():
                with sqlite3.connect('db/users.sqlite', timeout=30.0) as users_db:
                    return users_db.execute("SELECT fid, nickname FROM users").fetchall()
            users = await asyncio.to_thread(_read)

            choices = [
                discord.app_commands.Choice(name=f"{nickname} ({fid})", value=str(fid)) 
                for fid, nickname in users
            ]

            if current:
                filtered_choices = [choice for choice in choices if current.lower() in choice.name.lower()][:25]
            else:
                filtered_choices = choices[:25]

            return filtered_choices
        
        except Exception as e:
            logger.error(f"Autocomplete could not be loaded: {e}")
            print(f"Autocomplete could not be loaded: {e}")
            return []


    async def fetch_user_info(self, interaction: discord.Interaction, fid: str):
        try:
            await interaction.response.defer(thinking=True)

            # Stored profile only; no live lookup.
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute(
                    "SELECT nickname, kid, alliance, power, power_updated_at, combat_power, "
                    "combat_power_updated_at, discord_id, discord_id_updated_at "
                    "FROM users WHERE fid=?",
                    (fid,),
                )
                row = cursor.fetchone()

            if not row:
                await interaction.followup.send(
                    f"User with ID {fid} is not on the list. Live lookups are no longer "
                    f"available (the game removed the player API), so only added members can be shown."
                )
                return

            (nickname, kid, user_alliance, power_val, power_ts,
             combat_power_val, combat_power_ts, discord_id_val, discord_id_ts) = row

            alliance_info = None
            if user_alliance:
                with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                    acursor = alliance_db.cursor()
                    acursor.execute(
                        "SELECT name FROM alliance_list WHERE alliance_id=?",
                        (user_alliance,),
                    )
                    alliance_info = acursor.fetchone()

            embed = discord.Embed(
                title=f"{theme.userIcon} {nickname or f'Player {fid}'}",
                description=(
                    f"{theme.upperDivider}\n"
                    f"**{theme.fidIcon} ID:** `{fid}`\n"
                    f"**{theme.globeIcon} Kingdom:** `{kid if kid is not None else 'unknown'}`\n"
                    f"{theme.middleDivider}\n"
                ),
                color=theme.emColor1
            )

            if alliance_info:
                embed.description += f"**{theme.allianceIcon} Alliance:** `{alliance_info[0]}`\n"

            if power_val is not None:
                age = _relative_age(power_ts)
                age_suffix = f" · updated {age}" if age else ""
                embed.description += (
                    f"**{theme.boltIcon} Power:** `{_format_big_int(power_val)}`{age_suffix}\n"
                )
            if combat_power_val is not None:
                age = _relative_age(combat_power_ts)
                age_suffix = f" · updated {age}" if age else ""
                embed.description += (
                    f"**{theme.shieldIcon} Combat Power:** "
                    f"`{_format_big_int(combat_power_val)}`{age_suffix}\n"
                )

            if discord_id_val:
                linked_mention = f"<@{discord_id_val}>"
                age = _relative_age(discord_id_ts)
                age_suffix = f" · linked {age}" if age else ""
                embed.description += (
                    f"**{theme.chatIcon} Discord:** {linked_mention}{age_suffix}\n"
                )

            embed.description += f"{theme.lowerDivider}\n"
            embed.set_footer(text=f"Stored profile {theme.verifiedIcon}")

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error fetching user info for ID {fid}: {e}")
            print(f"An error occurred: {e}")
            await interaction.followup.send("An error occurred while fetching user info.")


async def setup(bot):
    await bot.add_cog(WCommand(bot))
