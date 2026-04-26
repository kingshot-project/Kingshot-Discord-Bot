import discord
from discord.ext import commands
import aiohttp
import hashlib
import ssl
import time
import asyncio
import sqlite3

class WCommand(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = sqlite3.connect('db/changes.sqlite')
        self.c = self.conn.cursor()
        self.SECRET = "mN4!pQs6JrYwV9"
        
        self.level_mapping = {
            31: "30-1", 32: "30-2", 33: "30-3", 34: "30-4",
            35: "TG 1", 36: "TG 1 - 1", 37: "TG 1 - 2", 38: "TG 1 - 3", 39: "TG 1 - 4",
            40: "TG 2", 41: "TG 2 - 1", 42: "TG 2 - 2", 43: "TG 2 - 3", 44: "TG 2 - 4",
            45: "TG 3", 46: "TG 3 - 1", 47: "TG 3 - 2", 48: "TG 3 - 3", 49: "TG 3 - 4",
            50: "TG 4", 51: "TG 4 - 1", 52: "TG 4 - 2", 53: "TG 4 - 3", 54: "TG 4 - 4",
            55: "TG 5", 56: "TG 5 - 1", 57: "TG 5 - 2", 58: "TG 5 - 3", 59: "TG 5 - 4",
            60: "TG 6", 61: "TG 6 - 1", 62: "TG 6 - 2", 63: "TG 6 - 3", 64: "TG 6 - 4",
            65: "TG 7", 66: "TG 7 - 1", 67: "TG 7 - 2", 68: "TG 7 - 3", 69: "TG 7 - 4",
            70: "TG 8", 71: "TG 8 - 1", 72: "TG 8 - 2", 73: "TG 8 - 3", 74: "TG 8 - 4",
            75: "TG 9", 76: "TG 9 - 1", 77: "TG 9 - 2", 78: "TG 9 - 3", 79: "TG 9 - 4",
            80: "TG 10", 81: "TG 10 - 1", 82: "TG 10 - 2", 83: "TG 10 - 3", 84: "TG 10 - 4"
        }

    def cog_unload(self):
        if hasattr(self, 'conn'):
            self.conn.close()

    @discord.app_commands.command(name='w', description='Fetches user info using fid.')
    async def w(self, interaction: discord.Interaction, fid: str):
        await self.fetch_user_info(interaction, fid)

    @w.autocomplete('fid')
    async def autocomplete_fid(self, interaction: discord.Interaction, current: str):
        try:
            with sqlite3.connect('db/users.sqlite') as users_db:
                cursor = users_db.cursor()
                cursor.execute("SELECT fid, nickname FROM users")
                users = cursor.fetchall()

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
            print(f"Autocomplete could not be loaded: {e}")
            return []


    async def fetch_user_info(self, interaction: discord.Interaction, fid: str):
        try:
            await interaction.response.defer(thinking=True)
            
            current_time = int(time.time() * 1000)
            form = f"fid={fid}&time={current_time}"
            sign = hashlib.md5((form + self.SECRET).encode('utf-8')).hexdigest()
            form = f"sign={sign}&{form}"

            url = 'https://kingshot-giftcode.centurygame.com/api/player'
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            max_retries = 3
            retry_delay = 60

            for attempt in range(max_retries):
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.post(url, headers=headers, data=form, ssl=ssl_context) as response:
                        if response.status == 200:
                            data = await response.json()
                            nickname = data['data']['nickname']
                            fid_value = data['data']['fid']
                            stove_level = data['data']['stove_lv']
                            kid = data['data']['kid']
                            avatar_image = data['data']['avatar_image']
                            stove_lv_content = data['data'].get('stove_lv_content')

                            if stove_level > 30:
                                stove_level_name = self.level_mapping.get(stove_level, f"Level {stove_level}")
                            else:
                                stove_level_name = f"Level {stove_level}"

                            user_info = None
                            alliance_info = None
                            
                            with sqlite3.connect('db/users.sqlite') as users_db:
                                cursor = users_db.cursor()
                                cursor.execute("SELECT *, alliance FROM users WHERE fid=?", (fid_value,))
                                user_info = cursor.fetchone()
                                
                                if user_info and user_info[-1]:
                                    with sqlite3.connect('db/alliance.sqlite') as alliance_db:
                                        cursor = alliance_db.cursor()
                                        cursor.execute("SELECT name FROM alliance_list WHERE alliance_id=?", (user_info[-1],))
                                        alliance_info = cursor.fetchone()

                            embed = discord.Embed(
                                title=f"👤 {nickname}",
                                description=(
                                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"**🆔 ID:** `{fid_value}`\n"
                                    f"**🏰 Castle Level:** `{stove_level_name}`\n"
                                    f"**🌍 Kingdom:** `{kid}`\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                                ),
                                color=discord.Color.blue()
                            )

                            if alliance_info:
                                embed.description += f"**🏰 Alliance:** `{alliance_info[0]}`\n━━━━━━━━━━━━━━━━━━━━━━\n"

                            registration_status = "Registered on the List ✅" if user_info else "Not on the List ❌"
                            embed.set_footer(text=registration_status)

                            if avatar_image:
                                embed.set_image(url=avatar_image)
                            if isinstance(stove_lv_content, str) and stove_lv_content.startswith("http"):
                                embed.set_thumbnail(url=stove_lv_content)

                            await interaction.followup.send(embed=embed)
                            return 

                        elif response.status == 429:
                            if attempt < max_retries - 1:
                                await interaction.followup.send("API limit reached, your result will be displayed automatically shortly...")
                                await asyncio.sleep(retry_delay)
            await interaction.followup.send(f"User with ID {fid} not found or an error occurred after multiple attempts.")
            
        except Exception as e:
            print(f"An error occurred: {e}")
            await interaction.followup.send("An error occurred while fetching user info.")


async def setup(bot):
    await bot.add_cog(WCommand(bot))
