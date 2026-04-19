import discord
import aiohttp
import asyncio
import os
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
    if not api_key:
        return None
    url = f"{BLOXLINK_BASE}/guilds/{guild_id}/discord-to-roblox/{discord_id}"
    data = await fetch_json(url, headers={"Authorization": api_key})
    if not data:
        return None
    rid = data.get("robloxID") or data.get("roblox_id")
    return int(rid) if rid else None


async def bloxlink_roblox_to_discord(roblox_id, guild_id, api_key):
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


# ─────────── rblx-open-cloud (optional, for auto-ranking) ───────────

async def roblox_set_rank(roblox_user_id, rank_name, api_key, group_id):
    """Set a user's rank in a Roblox group using rblx-open-cloud. Returns (ok, msg)."""
    if not api_key:
        return False, "ROBLOX_API_KEY not set"

    def _do():
        import rblxopencloud
        group = rblxopencloud.Group(group_id, api_key)
        target_role = None
        for role in group.list_roles():
            if role.name.lower() == rank_name.lower():
                target_role = role
                break
        if not target_role:
            raise ValueError(f"Rank '{rank_name}' not found in group {group_id}")
        group.update_member(roblox_user_id, target_role.id)

    try:
        await asyncio.to_thread(_do)
        return True, "OK"
    except ImportError:
        return False, "rblx-open-cloud not installed — run: pip install rblx-open-cloud"
    except Exception as e:
        return False, str(e)


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
    """Roblox account verification with Bloxlink + multi-guild role sync."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710042, force_registration=True)
        self.config.register_user(
            roblox_id=None,
            roblox_username=None,
            rank_name=None,
            verified_at=None,
        )
        self.config.register_guild(
            verified_role=None,
            apply_nickname=True,
            bloxlink_api_key=None,
            group_id=None,
            rank_role_bindings={},   # dict: rank_name -> role_id
            include_in_sync=True,    # whether to include this guild in cross-guild sync
        )

    # ─────────── Public methods (used by other cogs) ───────────

    async def get_verified_data(self, discord_id: int):
        """Return {'roblox_id', 'roblox_username', 'rank_name', 'verified_at'} or None."""
        try:
            user = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
        except discord.HTTPException:
            return None
        data = await self.config.user(user).all()
        if not data.get("roblox_id"):
            return None
        return data

    async def save_verified(self, discord_id: int, roblox_id, roblox_username: str, rank_name: str = None):
        """Persist a Discord ↔ Roblox link. rank_name is optional."""
        user = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
        await self.config.user(user).roblox_id.set(str(roblox_id))
        await self.config.user(user).roblox_username.set(roblox_username)
        await self.config.user(user).verified_at.set(datetime.utcnow().isoformat())
        if rank_name is not None:
            await self.config.user(user).rank_name.set(rank_name)

    async def apply_roles_in_guild(self, member: discord.Member, roblox_username: str, rank_name: str = None):
        """Apply verified role, rank role (if bound), and nickname in this one guild. Returns status string."""
        guild = member.guild
        cfg = await self.config.guild(guild).all()

        roles_to_add = []
        roles_to_remove = []

        # Verified role
        verified_role_id = cfg.get("verified_role")
        if verified_role_id:
            vr = guild.get_role(verified_role_id)
            if vr and vr not in member.roles:
                roles_to_add.append(vr)

        # Rank roles: remove all bound rank roles except the one that matches rank_name
        rank_bindings = cfg.get("rank_role_bindings") or {}
        target_rank_role = None
        if rank_name:
            target_rid = rank_bindings.get(rank_name)
            if target_rid:
                target_rank_role = guild.get_role(target_rid)

        for rname, rid in rank_bindings.items():
            role = guild.get_role(rid)
            if not role:
                continue
            if role == target_rank_role:
                if role not in member.roles:
                    roles_to_add.append(role)
            else:
                if role in member.roles:
                    roles_to_remove.append(role)

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason=f"Roblox sync: {rank_name or 'verified'}")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=f"Roblox sync: {rank_name or 'verified'}")
        except discord.Forbidden:
            return "No permission"
        except discord.HTTPException as e:
            return f"Error: {e}"

        # Nickname
        if cfg.get("apply_nickname", True) and guild.me and guild.me.guild_permissions.manage_nicknames:
            try:
                if member != guild.owner and member.top_role < guild.me.top_role:
                    await member.edit(nick=roblox_username, reason="Roblox verified")
            except discord.HTTPException:
                pass

        return "OK"

    async def apply_roles_everywhere(self, discord_id: int, roblox_username: str, rank_name: str = None):
        """
        Apply roles and nickname across every guild the bot is in that has:
          - verified_role set, AND
          - include_in_sync = True
        Returns dict: {guild_name: status}
        """
        results = {}
        for guild in self.bot.guilds:
            cfg = await self.config.guild(guild).all()
            if not cfg.get("verified_role"):
                continue
            if not cfg.get("include_in_sync", True):
                continue
            member = guild.get_member(discord_id)
            if not member:
                results[guild.name] = "Not in server"
                continue
            results[guild.name] = await self.apply_roles_in_guild(member, roblox_username, rank_name)
        return results

    async def rank_on_roblox(self, roblox_id: int, rank_name: str, guild: discord.Guild = None, group_id: int = None):
        """
        Set a user's rank on a Roblox group via rblx-open-cloud.
        group_id defaults to the configured group for `guild` if not provided.
        Requires ROBLOX_API_KEY env var.
        """
        if group_id is None and guild is not None:
            group_id = await self.config.guild(guild).group_id()
        if not group_id:
            return False, "No Roblox group_id configured"
        api_key = os.environ.get("ROBLOX_API_KEY", "")
        return await roblox_set_rank(roblox_id, rank_name, api_key, group_id)

    # ─────────── Internal ───────────

    async def _auto_sync_after_verify(self, discord_id: int, roblox_username: str, rank_name: str = None):
        """Called after a verify — runs role/nick sync across all configured guilds."""
        await self.apply_roles_everywhere(discord_id, roblox_username, rank_name)

    # ─────────── User commands ───────────

    @commands.hybrid_command(name="rverify", description="Verify your Roblox account (tries Bloxlink first)")
    async def rverify(self, ctx: commands.Context, *, username: str = None):
        """
        Verify your Roblox account.

        If your server has Bloxlink configured and you're already Bloxlink-verified,
        it links instantly. Otherwise provide your Roblox username to do profile-code
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
                    group_id = await self.config.guild(ctx.guild).group_id()
                    rank_info = await roblox_group_rank(roblox_id, group_id) if group_id else None
                    rank_name = rank_info["name"] if rank_info else None

                    await self.save_verified(ctx.author.id, roblox_id, roblox_name, rank_name)
                    await self._auto_sync_after_verify(ctx.author.id, roblox_name, rank_name)

                    embed = discord.Embed(
                        title="Verified via Bloxlink",
                        description=(
                            f"**Roblox:** {roblox_name}\n"
                            f"**ID:** `{roblox_id}`\n"
                            + (f"**Group rank:** {rank_name}\n" if rank_name else "")
                            + f"[Profile](https://www.roblox.com/users/{roblox_id}/profile)"
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
        if view.verified:
            # Optionally fetch group rank at this point too
            group_id = await self.config.guild(ctx.guild).group_id()
            rank_info = await roblox_group_rank(roblox_id, group_id) if group_id else None
            rank_name = rank_info["name"] if rank_info else None
            if rank_name:
                await self.save_verified(ctx.author.id, roblox_id, roblox_name, rank_name)
            await self._auto_sync_after_verify(ctx.author.id, roblox_name, rank_name)

    @commands.hybrid_command(name="whois", description="Show the Roblox account linked to a Discord user")
    async def whois(self, ctx: commands.Context, member: discord.Member = None):
        """Show a member's linked Roblox account."""
        member = member or ctx.author
        data = await self.config.user(member).all()

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
                            "rank_name": None,
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

        if ctx.guild:
            group_id = await self.config.guild(ctx.guild).group_id()
            if group_id:
                rank = await roblox_group_rank(int(roblox_id), group_id)
                if rank:
                    embed.add_field(name="Group Rank", value=f"{rank['name']} ({rank['rank']})", inline=False)
                elif data.get("rank_name"):
                    embed.add_field(name="Stored rank", value=data["rank_name"], inline=False)

        avatar = await roblox_avatar(int(roblox_id))
        if avatar:
            embed.set_thumbnail(url=avatar)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="whoisroblox", description="Find who a Roblox ID belongs to in this server")
    async def whoisroblox(self, ctx: commands.Context, roblox_id: int):
        """Reverse lookup via Bloxlink."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        api_key = await self.config.guild(ctx.guild).bloxlink_api_key()
        if not api_key:
            return await ctx.send("Bloxlink reverse lookup isn't configured.", ephemeral=True)

        ids = await bloxlink_roblox_to_discord(roblox_id, ctx.guild.id, api_key)
        if not ids:
            return await ctx.send(f"No Discord users linked to Roblox ID `{roblox_id}`.", ephemeral=True)
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
            return await ctx.send("Bloxlink isn't configured.", ephemeral=True)

        member = member or ctx.author
        ok = await bloxlink_update_user(member.id, ctx.guild.id, api_key)
        if ok:
            await ctx.send(f"✅ Bloxlink re-synced {member.mention}.")
        else:
            await ctx.send(f"❌ Bloxlink re-sync failed for {member.mention}.")

    @commands.hybrid_command(name="rupdate", description="Re-sync your Roblox roles/rank everywhere")
    async def rupdate(self, ctx: commands.Context, member: discord.Member = None):
        """Refresh a user's roles, rank, and nickname across all configured guilds."""
        member = member or ctx.author
        data = await self.config.user(member).all()
        if not data.get("roblox_id"):
            return await ctx.send(f"{member.mention} isn't verified yet.", ephemeral=True)

        roblox_id = int(data["roblox_id"])
        username = data.get("roblox_username") or str(roblox_id)

        # Re-fetch group rank if group is configured
        rank_name = data.get("rank_name")
        if ctx.guild:
            group_id = await self.config.guild(ctx.guild).group_id()
            if group_id:
                rank_info = await roblox_group_rank(roblox_id, group_id)
                if rank_info:
                    rank_name = rank_info["name"]
                    await self.config.user(member).rank_name.set(rank_name)

        results = await self.apply_roles_everywhere(member.id, username, rank_name)
        results_text = "\n".join(f"{g}: {s}" for g, s in results.items()) or "No guilds configured."
        embed = discord.Embed(title="Updated", color=0x57f287)
        embed.add_field(name="User", value=member.mention, inline=True)
        embed.add_field(name="Rank", value=rank_name or "—", inline=True)
        embed.add_field(name="Synced in", value=results_text, inline=False)
        await ctx.send(embed=embed)

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
        """Set or clear this server's Bloxlink API key. Get one at https://blox.link/dashboard/user/developer"""
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
        """Set (or clear) the role given to verified users."""
        if role is None:
            await self.config.guild(ctx.guild).verified_role.set(None)
            return await ctx.send("Verified role cleared.")
        await self.config.guild(ctx.guild).verified_role.set(role.id)
        await ctx.send(f"Verified role set to **{role.name}**.")

    @rverifyset.command(name="nickname")
    async def set_nick(self, ctx: commands.Context, on_off: bool):
        """Toggle auto-setting nicknames to Roblox usernames."""
        await self.config.guild(ctx.guild).apply_nickname.set(on_off)
        await ctx.send(f"Nickname on verify: **{'on' if on_off else 'off'}**.")

    @rverifyset.command(name="group")
    async def set_group(self, ctx: commands.Context, group_id: int = None):
        """Set the Roblox group ID to track ranks (0 to clear)."""
        if group_id in (None, 0):
            await self.config.guild(ctx.guild).group_id.set(None)
            return await ctx.send("Group tracking cleared.")
        await self.config.guild(ctx.guild).group_id.set(group_id)
        await ctx.send(f"Group ID set to `{group_id}`.")

    @rverifyset.command(name="rankrole")
    async def set_rank_role(self, ctx: commands.Context, rank_name: str, role: discord.Role = None):
        """Bind a Roblox rank name to a Discord role. Pass no role to clear the binding.

        Example: `[p]rverifyset rankrole Newborn @Newborn`
        """
        async with self.config.guild(ctx.guild).rank_role_bindings() as bindings:
            if role is None:
                if rank_name in bindings:
                    del bindings[rank_name]
                    return await ctx.send(f"Cleared binding for rank `{rank_name}`.")
                return await ctx.send(f"No binding exists for rank `{rank_name}`.")
            bindings[rank_name] = role.id
        await ctx.send(f"Bound rank **{rank_name}** → {role.mention}.")

    @rverifyset.command(name="syncinclude")
    async def set_sync(self, ctx: commands.Context, on_off: bool):
        """Include this server in cross-guild role sync (default: on)."""
        await self.config.guild(ctx.guild).include_in_sync.set(on_off)
        await ctx.send(f"Cross-guild sync for this server: **{'on' if on_off else 'off'}**.")

    @rverifyset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current settings for this server."""
        data = await self.config.guild(ctx.guild).all()
        role = ctx.guild.get_role(data["verified_role"]) if data["verified_role"] else None

        bindings_text = "None"
        if data.get("rank_role_bindings"):
            lines = []
            for rname, rid in data["rank_role_bindings"].items():
                r = ctx.guild.get_role(rid)
                lines.append(f"• {rname} → {r.mention if r else f'`{rid}` (missing)'}")
            bindings_text = "\n".join(lines)

        embed = discord.Embed(title="RobloxVerify Settings", color=0x5865f2)
        embed.add_field(name="Verified role", value=role.mention if role else "Not set", inline=False)
        embed.add_field(name="Apply nickname", value="Yes" if data["apply_nickname"] else "No", inline=False)
        embed.add_field(name="Group ID", value=str(data["group_id"]) if data["group_id"] else "Not set", inline=False)
        embed.add_field(name="Bloxlink key", value="✅ Set" if data["bloxlink_api_key"] else "❌ Not set", inline=False)
        embed.add_field(name="Include in cross-guild sync", value="Yes" if data.get("include_in_sync", True) else "No", inline=False)
        embed.add_field(name="Rank → Role bindings", value=bindings_text, inline=False)
        await ctx.send(embed=embed)    
