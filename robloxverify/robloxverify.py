import discord
import aiohttp
import random
from datetime import datetime
from redbot.core import commands, Config
from redbot.core.bot import Red


BLOXLINK_BASE = "https://api.blox.link/v4/public"

WORDS = [
    "apple", "tiger", "moon", "crown", "river", "phoenix", "shadow",
    "ember", "frost", "storm", "raven", "willow", "thorn", "crystal",
    "dawn", "dusk", "silver", "copper", "iron", "oak", "flame", "mist",
]


def generate_code() -> str:
    w1, w2 = random.sample(WORDS, 2)
    n = random.randint(10, 99)
    return f"{w1}-{w2}-{n}"


# ─────────── Roblox API helpers ───────────

async def fetch_json(url, method="GET", json_body=None, headers=None):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.request(method, url, json=json_body, headers=headers or {}) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception:
        return None


async def roblox_user_by_name(username):
    data = await fetch_json(
        "https://users.roblox.com/v1/usernames/users",
        method="POST",
        json_body={"usernames": [username], "excludeBannedUsers": False},
    )
    if not data or not data.get("data"):
        return None
    return data["data"][0]


async def roblox_user_by_id(roblox_id):
    return await fetch_json(f"https://users.roblox.com/v1/users/{roblox_id}")


async def roblox_avatar(roblox_id):
    data = await fetch_json(
        f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={roblox_id}&size=150x150&format=Png"
    )
    if data and data.get("data"):
        return data["data"][0].get("imageUrl")
    return None


async def roblox_group_rank(roblox_id, group_id):
    data = await fetch_json(f"https://groups.roblox.com/v1/users/{roblox_id}/groups/roles")
    if not data:
        return None
    for g in data.get("data", []):
        if g["group"]["id"] == group_id:
            return {"name": g["role"]["name"], "rank": g["role"]["rank"]}
    return None


# ─────────── Bloxlink helpers ───────────

async def bloxlink_discord_to_roblox(discord_id, guild_id, api_key):
    """Returns roblox_id (int) or None."""
    if not api_key:
        return None
    url = f"{BLOXLINK_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    data = await fetch_json(url, headers={"Authorization": api_key})
    if not data:
        return None
    rid = data.get("robloxID") or data.get("roblox_id")
    return int(rid) if rid else None


async def bloxlink_roblox_to_discord(roblox_id, guild_id, api_key):
    """Returns list of discord ids (ints)."""
    if not api_key:
        return []
    url = f"{BLOXLINK_BASE}/guilds/{guild_id}/roblox-to-discord/{roblox_id}"
    data = await fetch_json(url, headers={"Authorization": api_key})
    if not data:
        return []
    ids = data.get("matchedDiscordIDs") or data.get("discordIDs") or []
    return [int(i) for i in ids]


async def bloxlink_update_user(discord_id, guild_id, api_key):
    if not api_key:
        return False
    url = f"{BLOXLINK_BASE}/guilds/{guild_id}/update-user/{discord_id}"
    data = await fetch_json(url, method="POST", json_body={}, headers={"Authorization": api_key})
    return data is not None


# ─────────── UI ───────────

class ConfirmView(discord.ui.View):
    def __init__(self, cog, user, roblox_id, roblox_username, code):
        super().__init__(timeout=600)
        self.cog = cog
        self.user = user
        self.roblox_id = roblox_id
        self.roblox_username = roblox_username
        self.code = code
        self.verified = False

    @discord.ui.button(label="✅ I added the code", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("Not your verification.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        user_data = await roblox_user_by_id(self.roblox_id)
        if not user_data:
            return await interaction.followup.send("Couldn't reach Roblox. Try again.", ephemeral=True)

        desc = user_data.get("description", "") or ""
        if self.code not in desc:
            return await interaction.followup.send(
                f"Code `{self.code}` not found in your profile description. "
                f"Save it on Roblox, then click again.",
                ephemeral=True,
            )

        await self.cog.save_verified(self.user.id, self.roblox_id, self.roblox_username)
        self.verified = True
        for c in self.children:
            c.disabled = True
        await interaction.followup.send(
            f"✅ Verified! Linked to **{self.roblox_username}**. "
            f"You can remove the code from your profile now.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("Not your verification.", ephemeral=True)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()


# ─────────── Cog ───────────

class RobloxVerify(commands.Cog):
    """Roblox account verification with optional Bloxlink integration."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710042, force_registration=True)
        self.config.register_user(
            roblox_id=None,
            roblox_username=None,
            verified_at=None,
        )
        self.config.register_guild(
            verified_role=None,
            apply_nickname=True,
            bloxlink_api_key=None,
            group_id=None,  # optional: roblox group id to track rank
        )

    # ─────────── Storage ───────────

    async def save_verified(self, discord_id, roblox_id, roblox_username):
        user = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
        await self.config.user(user).roblox_id.set(str(roblox_id))
        await self.config.user(user).roblox_username.set(roblox_username)
        await self.config.user(user).verified_at.set(datetime.utcnow().isoformat())

    async def _apply_role_and_nick(self, member: discord.Member, username: str):
        guild = member.guild
        role_id = await self.config.guild(guild).verified_role()
        do_nick = await self.config.guild(guild).apply_nickname()

        if role_id:
            role = guild.get_role(role_id)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Roblox verified")
                except discord.HTTPException:
                    pass

        if do_nick and guild.me and guild.me.guild_permissions.manage_nicknames:
            try:
                if member.top_role < guild.me.top_role and member != guild.owner:
                    await member.edit(nick=username, reason="Roblox verified")
            except discord.HTTPException:
                pass

    # ─────────── User commands ───────────

    @commands.hybrid_command(name="rverify", description="Verify your Roblox account (tries Bloxlink first)")
    async def rverify(self, ctx: commands.Context, *, username: str = None):
        """
        Verify your Roblox account.

        If your server has Bloxlink configured and you're already Bloxlink-verified,
        it links instantly. Otherwise, provide your Roblox username to do profile-code
        verification.
        """
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")

        # Step 1: Try Bloxlink
        api_key = await self.config.guild(ctx.guild).bloxlink_api_key()
        if api_key:
            roblox_id = await bloxlink_discord_to_roblox(ctx.author.id, ctx.guild.id, api_key)
            if roblox_id:
                user_data = await roblox_user_by_id(roblox_id)
                if user_data:
                    roblox_name = user_data.get("name", str(roblox_id))
                    await self.save_verified(ctx.author.id, roblox_id, roblox_name)
                    if isinstance(ctx.author, discord.Member):
                        await self._apply_role_and_nick(ctx.author, roblox_name)
                    embed = discord.Embed(
                        title="Verified via Bloxlink",
                        description=(
                            f"**Roblox:** {roblox_name}\n"
                            f"**ID:** `{roblox_id}`\n"
                            f"[Profile](https://www.roblox.com/users/{roblox_id}/profile)"
                        ),
                        color=0x57f287,
                    )
                    avatar = await roblox_avatar(roblox_id)
                    if avatar:
                        embed.set_thumbnail(url=avatar)
                    return await ctx.send(embed=embed, ephemeral=True)

        # Step 2: Fall back to profile-code verification
        if not username:
            msg = "You're not Bloxlink-verified in this server. " if api_key else ""
            return await ctx.send(
                f"{msg}To verify manually, run `{ctx.prefix}rverify <your_roblox_username>`.",
                ephemeral=True,
            )

        user_info = await roblox_user_by_name(username)
        if not user_info:
            return await ctx.send(f"No Roblox user named `{username}` found.", ephemeral=True)

        roblox_id = user_info["id"]
        roblox_name = user_info["name"]
        code = generate_code()

        embed = discord.Embed(
            title="Step 2 — Add this code to your Roblox profile",
            description=(
                f"**Roblox:** {roblox_name} (`{roblox_id}`)\n\n"
                f"**Your code:**\n```\n{code}\n```\n"
                f"1. Open https://www.roblox.com/users/{roblox_id}/profile\n"
                f"2. Click the pencil icon by your username\n"
                f"3. Paste the code into your **About** section, **Save**\n"
                f"4. Click ✅ below\n\n"
                f"You can remove the code after verification."
            ),
            color=0x5865f2,
        )
        avatar = await roblox_avatar(roblox_id)
        if avatar:
            embed.set_thumbnail(url=avatar)
        embed.set_footer(text="Code expires in 10 minutes")

        view = ConfirmView(self, ctx.author, roblox_id, roblox_name, code)
        await ctx.send(embed=embed, view=view, ephemeral=True)

        await view.wait()
        if view.verified and isinstance(ctx.author, discord.Member):
            await self._apply_role_and_nick(ctx.author, roblox_name)

    @commands.hybrid_command(name="whois", description="Show the Roblox account linked to a Discord user")
    async def whois(self, ctx: commands.Context, member: discord.Member = None):
        """Show a member's linked Roblox account."""
        member = member or ctx.author
        data = await self.config.user(member).all()

        # Fall back to Bloxlink if not in our DB
        if not data.get("roblox_id") and ctx.guild:
            api_key = await self.config.guild(ctx.guild).bloxlink_api_key()
            if api_key:
                roblox_id = await bloxlink_discord_to_roblox(member.id, ctx.guild.id, api_key)
                if roblox_id:
                    user_data = await roblox_user_by_id(roblox_id)
                    if user_data:
                        data = {
                            "roblox_id": str(roblox_id),
                            "roblox_username": user_data.get("name"),
                        }

        if not data.get("roblox_id"):
            return await ctx.send(f"{member.mention} is not verified.", ephemeral=True)

        roblox_id = data["roblox_id"]
        username = data.get("roblox_username") or "Unknown"
        embed = discord.Embed(
            title=f"{member.display_name}'s Roblox",
            description=(
                f"**Username:** {username}\n"
                f"**ID:** `{roblox_id}`\n"
                f"[Profile](https://www.roblox.com/users/{roblox_id}/profile)"
            ),
            color=0x57f287,
        )

        # Include group rank if configured
        if ctx.guild:
            group_id = await self.config.guild(ctx.guild).group_id()
            if group_id:
                rank = await roblox_group_rank(int(roblox_id), group_id)
                if rank:
                    embed.add_field(name="Group Rank", value=f"{rank['name']} ({rank['rank']})", inline=False)

        avatar = await roblox_avatar(int(roblox_id))
        if avatar:
            embed.set_thumbnail(url=avatar)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="whoisroblox", description="Find who a Roblox ID belongs to in this server")
    async def whoisroblox(self, ctx: commands.Context, roblox_id: int):
        """Reverse lookup: find Discord users linked to a Roblox ID (uses Bloxlink)."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        api_key = await self.config.guild(ctx.guild).bloxlink_api_key()
        if not api_key:
            return await ctx.send("Bloxlink reverse lookup isn't configured on this server.", ephemeral=True)

        ids = await bloxlink_roblox_to_discord(roblox_id, ctx.guild.id, api_key)
        if not ids:
            return await ctx.send(f"No Discord users found linked to Roblox ID `{roblox_id}`.", ephemeral=True)

        mentions = " ".join(f"<@{i}>" for i in ids)
        await ctx.send(f"Roblox `{roblox_id}` → {mentions}")

    @commands.hybrid_command(name="bloxforce", description="Re-sync a user with Bloxlink")
    @commands.admin_or_permissions(manage_guild=True)
    async def bloxforce(self, ctx: commands.Context, member: discord.Member = None):
        """Force Bloxlink to re-evaluate a user's roles and nickname."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        api_key = await self.config.guild(ctx.guild).bloxlink_api_key()
        if not api_key:
            return await ctx.send("Bloxlink isn't configured on this server.", ephemeral=True)

        member = member or ctx.author
        ok = await bloxlink_update_user(member.id, ctx.guild.id, api_key)
        if ok:
            await ctx.send(f"✅ Bloxlink re-synced {member.mention}.")
        else:
            await ctx.send(f"❌ Bloxlink re-sync failed for {member.mention}.")

    @commands.hybrid_command(name="unverify", description="Remove your Roblox verification")
    async def unverify(self, ctx: commands.Context):
        await self.config.user(ctx.author).clear()
        await ctx.send("Your Roblox verification has been removed.", ephemeral=True)

    # ─────────── Admin: rverifyset ───────────

    @commands.group(name="rverifyset")
    @commands.admin_or_permissions(manage_guild=True)
    async def rverifyset(self, ctx: commands.Context):
        """Configure RobloxVerify for this server."""

    @rverifyset.command(name="bloxlinkkey")
    async def set_bloxlink_key(self, ctx: commands.Context, *, api_key: str = None):
        """Set (or clear) this server's Bloxlink API key.

        Get one at https://blox.link/dashboard/user/developer
        Tip: delete your message afterwards to keep the key private.
        """
        if api_key is None:
            await self.config.guild(ctx.guild).bloxlink_api_key.set(None)
            return await ctx.send("Bloxlink API key cleared.")
        await self.config.guild(ctx.guild).bloxlink_api_key.set(api_key.strip())
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        await ctx.send("✅ Bloxlink API key saved. (Your message was deleted for privacy.)")

    @rverifyset.command(name="role")
    async def set_role(self, ctx: commands.Context, role: discord.Role = None):
        """Set the role given to verified users."""
        if role is None:
            await self.config.guild(ctx.guild).verified_role.set(None)
            return await ctx.send("Verified role cleared.")
        await self.config.guild(ctx.guild).verified_role.set(role.id)
        await ctx.send(f"Verified role set to **{role.name}**.")

    @rverifyset.command(name="nickname")
    async def set_nick(self, ctx: commands.Context, on_off: bool):
        """Toggle auto-setting verified users' nicknames to their Roblox username."""
        await self.config.guild(ctx.guild).apply_nickname.set(on_off)
        await ctx.send(f"Nickname on verify: **{'on' if on_off else 'off'}**.")

    @rverifyset.command(name="group")
    async def set_group(self, ctx: commands.Context, group_id: int = None):
        """Set a Roblox group ID to show rank info in /whois (0 to clear)."""
        if group_id in (None, 0):
            await self.config.guild(ctx.guild).group_id.set(None)
            return await ctx.send("Group tracking cleared.")
        await self.config.guild(ctx.guild).group_id.set(group_id)
        await ctx.send(f"Group ID set to `{group_id}`.")

    @rverifyset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current settings for this server."""
        data = await self.config.guild(ctx.guild).all()
        role = ctx.guild.get_role(data["verified_role"]) if data["verified_role"] else None
        embed = discord.Embed(title="RobloxVerify Settings", color=0x5865f2)
        embed.add_field(name="Verified role", value=role.mention if role else "Not set", inline=False)
        embed.add_field(name="Apply nickname", value="Yes" if data["apply_nickname"] else "No", inline=False)
        embed.add_field(name="Group ID", value=str(data["group_id"]) if data["group_id"] else "Not set", inline=False)
        embed.add_field(name="Bloxlink key", value="✅ Set" if data["bloxlink_api_key"] else "❌ Not set", inline=False)
        await ctx.send(embed=embed)
