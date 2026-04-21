import discord
import aiohttp
import asyncio
from datetime import datetime, timezone
from redbot.core import commands, Config
from redbot.core.bot import Red


# ─────────── Hardcoded section layout ───────────
# Edit this list when the hierarchy changes. rank_top / rank_second are the
# ACTUAL Roblox rank VALUES (not ordinal positions).
SECTIONS = [
    {
        "title": "The Wulfgard (Combative)",
        "prefix_name": True,
        "groups": [
            {"name": "Wulfgard",        "group_id": 689853179, "rank_top": 8,  "rank_second": 7,  "label_top": "Ascendant", "label_second": "Gibbous"},
            {"name": "Ravager",         "group_id": 479414319, "rank_top": 11, "rank_second": 10, "label_top": "Ascendant", "label_second": "Gibbous"},
            {"name": "Eviscerater",     "group_id": 201749000, "rank_top": 11, "rank_second": 10, "label_top": "Ascendant", "label_second": "Gibbous"},
            {"name": "Drifting Seeker", "group_id": 988037427, "rank_top": 11, "rank_second": 10, "label_top": "Ascendant", "label_second": "Gibbous"},
        ],
    },
    {
        "title": "The Sanctum (Rituals & Events)",
        "prefix_name": False,
        "groups": [
            {"name": "Sanctum",   "group_id": 261400171, "rank_top": 6, "rank_second": 5, "label_top": "Ascendant", "label_second": "Gibbous"},
        ],
    },
    {
        "title": "The Halvari (Relations)",
        "prefix_name": False,
        "groups": [
            {"name": "Halvari",   "group_id": 341490152, "rank_top": 6, "rank_second": 5, "label_top": "Ascendant", "label_second": "Gibbous"},
        ],
    },
    {
        "title": "The Athenaeum (Lore & Research)",
        "prefix_name": False,
        "groups": [
            {"name": "Athenaeum", "group_id": 693899217, "rank_top": 6, "rank_second": 5, "label_top": "Ascendant", "label_second": "Gibbous"},
        ],
    },
    {
        "title": "The Verdikari (Law Enforcement)",
        "prefix_name": False,
        "groups": [
            {"name": "Verdikari", "group_id": 128380705, "rank_top": 6, "rank_second": 5, "label_top": "Ascendant", "label_second": "Gibbous"},
        ],
    },
]


# ─────────── API helpers ───────────

async def fetch_json(url, headers=None):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, headers=headers or {}) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception as e:
        print(f"[leadership] fetch_json error: {e}")
        return None


async def get_role_id_by_rank_value(group_id, rank_value):
    """Return (role_id, rank_name) for a specific rank value in a group."""
    data = await fetch_json(f"https://groups.roblox.com/v1/groups/{group_id}/roles")
    if not data:
        return None, None
    for role in data.get("roles", []):
        if role["rank"] == rank_value:
            return role["id"], role["name"]
    return None, None


async def get_roblox_ids_by_rank(group_id, rank_value):
    """Return list of Roblox user IDs holding the given rank value in a group."""
    role_id, _ = await get_role_id_by_rank_value(group_id, rank_value)
    if not role_id:
        return []

    members = []
    cursor = ""
    while True:
        url = f"https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users?limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        data = await fetch_json(url)
        if not data:
            break
        for m in data.get("data", []):
            uid = str(m.get("userId", ""))
            if uid:
                members.append(uid)
        cursor = data.get("nextPageCursor", "")
        if not cursor:
            break
    return members


async def get_roblox_username(roblox_id):
    data = await fetch_json(f"https://users.roblox.com/v1/users/{roblox_id}")
    return data.get("name", str(roblox_id)) if data else str(roblox_id)


# ─────────── Cog ───────────

class MoonlightLeadership(commands.Cog):
    """Auto-updating leadership board showing top-rank holders in each Roblox sub-group."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710045, force_registration=True)
        self.config.register_guild(
            is_main_guild=False,             # only one guild should be marked main
            leadership_channel_id=None,
            leadership_message_id=None,
            saint_role_id=None,
            luminaries_role_id=None,
            trigger_role_ids=[],             # role changes that force a rebuild
            poll_interval=300,               # seconds between Roblox polls
        )

        self._update_task = None
        self._poll_task = None
        self._rank_cache = {}  # {group_id: {rank_value: set(roblox_ids)}}

    async def cog_load(self):
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    async def cog_unload(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._update_task:
            self._update_task.cancel()

    # ─────────── Main guild discovery ───────────

    async def _get_main_guild(self):
        """Return the guild marked as main, or None."""
        for guild in self.bot.guilds:
            if await self.config.guild(guild).is_main_guild():
                return guild
        return None

    # ─────────── Member resolution ───────────

    async def _resolve_discord_id_from_roblox(self, roblox_id):
        """Use RobloxVerify's stored data to find a Discord user for this Roblox ID."""
        verify_cog = self.bot.get_cog("RobloxVerify")
        if verify_cog is None:
            return None
        # Iterate all users in RobloxVerify's config — look for matching roblox_id
        try:
            all_users = await verify_cog.config.all_users()
        except Exception:
            return None
        for discord_id_str, data in all_users.items():
            if str(data.get("roblox_id")) == str(roblox_id):
                try:
                    return int(discord_id_str)
                except (TypeError, ValueError):
                    continue
        return None

    async def _resolve_mentions(self, guild: discord.Guild, roblox_ids):
        mentions = []
        for rid in roblox_ids:
            discord_id = await self._resolve_discord_id_from_roblox(rid)
            if discord_id:
                member = guild.get_member(discord_id)
                mentions.append(member.mention if member else f"<@{discord_id}>")
            else:
                username = await get_roblox_username(rid)
                mentions.append(f"**{username}**")
        return mentions

    # ─────────── Embed building ───────────

    async def _build_embed(self, guild: discord.Guild):
        cfg = await self.config.guild(guild).all()

        embed = discord.Embed(
            title="Leadership",
            description="Leadership hierarchy based on Roblox group ranks.",
            color=0x2b2d31,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        # Saint / Luminaries from local Discord roles (if configured)
        if cfg.get("saint_role_id"):
            saint_role = guild.get_role(cfg["saint_role_id"])
            saint_text = "\n".join(m.mention for m in saint_role.members) if saint_role and saint_role.members else "Vacant"
            embed.add_field(name="**The Saint**", value=saint_text, inline=False)

        if cfg.get("luminaries_role_id"):
            lum_role = guild.get_role(cfg["luminaries_role_id"])
            lum_text = "\n".join(m.mention for m in lum_role.members) if lum_role and lum_role.members else "Vacant"
            embed.add_field(name="**The Luminaries**", value=lum_text, inline=False)

        # Sections from Roblox API
        for section in SECTIONS:
            lines = []
            for g in section["groups"]:
                try:
                    top_ids = await get_roblox_ids_by_rank(g["group_id"], g["rank_top"])
                    second_ids = await get_roblox_ids_by_rank(g["group_id"], g["rank_second"])

                    top_mentions = await self._resolve_mentions(guild, top_ids)
                    second_mentions = await self._resolve_mentions(guild, second_ids)

                    top_display = ", ".join(top_mentions) if top_mentions else "Vacant"
                    second_display = ", ".join(second_mentions) if second_mentions else "Vacant"

                    if section["prefix_name"]:
                        lines.append(f"{g['name']} {g['label_top']}, {top_display}")
                        lines.append(f"{g['name']} {g['label_second']}, {second_display}")
                    else:
                        lines.append(f"{g['label_top']}, {top_display}")
                        lines.append(f"{g['label_second']}, {second_display}")
                except Exception as e:
                    lines.append(f"{g['name']}, *Error: {e}*")

            value = "\n".join(lines) or "Vacant"
            if len(value) > 1024:
                value = value[:1021] + "..."
            embed.add_field(name=f"**{section['title']}**", value=value, inline=False)

        date_str = datetime.now(timezone.utc).strftime("%d/%m/%y")
        embed.set_footer(text=f"Children of The Moonlight • Last updated {date_str}")
        return embed

    # ─────────── Message update ───────────

    async def _update_message(self, guild: discord.Guild):
        cfg = await self.config.guild(guild).all()
        channel_id = cfg.get("leadership_channel_id")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return

        embed = await self._build_embed(guild)

        # Try editing the existing message; else post a new one
        saved_id = cfg.get("leadership_message_id")
        if saved_id:
            try:
                msg = await channel.fetch_message(saved_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                return

        try:
            msg = await channel.send(embed=embed)
            await self.config.guild(guild).leadership_message_id.set(msg.id)
        except discord.HTTPException as e:
            print(f"[leadership] Failed to send message: {e}")

    def _debounced_update(self, guild: discord.Guild):
        """Schedule an update a few seconds out, cancelling any pending one."""
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
        self._update_task = self.bot.loop.create_task(self._delayed_update(guild))

    async def _delayed_update(self, guild: discord.Guild):
        try:
            await asyncio.sleep(2)
            await self._update_message(guild)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[leadership] Delayed update error: {e}")

    # ─────────── Poll loop ───────────

    async def _poll_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                guild = await self._get_main_guild()
                if guild is None:
                    await asyncio.sleep(60)
                    continue

                interval = await self.config.guild(guild).poll_interval()
                await asyncio.sleep(max(60, int(interval)))

                changed = False
                for section in SECTIONS:
                    for g in section["groups"]:
                        gid = g["group_id"]
                        cache_for_group = self._rank_cache.setdefault(gid, {})
                        for rank_val in (g["rank_top"], g["rank_second"]):
                            ids = await get_roblox_ids_by_rank(gid, rank_val)
                            new_set = set(ids)
                            if new_set != cache_for_group.get(rank_val):
                                changed = True
                            cache_for_group[rank_val] = new_set

                if changed:
                    self._debounced_update(guild)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[leadership] Poll error: {e}")
                await asyncio.sleep(60)

    # ─────────── Listeners ───────────

    @commands.Cog.listener()
    async def on_ready(self):
        guild = await self._get_main_guild()
        if guild:
            self._debounced_update(guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = await self._get_main_guild()
        if not guild or after.guild.id != guild.id:
            return
        trigger_ids = set(await self.config.guild(guild).trigger_role_ids())
        if not trigger_ids:
            return
        changed = {r.id for r in before.roles} ^ {r.id for r in after.roles}
        if changed & trigger_ids:
            self._debounced_update(guild)

    # ─────────── Commands ───────────

    @commands.command(name="leadership")
    @commands.guild_only()
    async def leadership_cmd(self, ctx: commands.Context):
        """Force-rebuild the leadership board now."""
        main_guild = await self._get_main_guild()
        if main_guild is None:
            return await ctx.send(
                "No main guild is configured yet. Run `[p]leadershipset main` in your main server.",
            )
        try:
            await ctx.channel.typing()
            await self._update_message(main_guild)
            await ctx.message.add_reaction("✅")
        except Exception as e:
            await ctx.send(f"❌ Error: `{e}`")

    # ─────────── Admin: leadershipset ───────────

    @commands.group(name="leadershipset")
    @commands.admin_or_permissions(manage_guild=True)
    async def leadershipset(self, ctx: commands.Context):
        """Configure MoonlightLeadership."""

    @leadershipset.command(name="main")
    async def set_main(self, ctx: commands.Context, on_off: bool = True):
        """Mark or unmark this server as the main guild (where the board lives)."""
        # If turning on, clear other guilds' main flag
        if on_off:
            for g in self.bot.guilds:
                if g.id != ctx.guild.id:
                    await self.config.guild(g).is_main_guild.set(False)
        await self.config.guild(ctx.guild).is_main_guild.set(on_off)
        await ctx.send(f"Main guild flag for this server: **{'on' if on_off else 'off'}**.")

    @leadershipset.command(name="channel")
    async def set_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the channel for the leadership board."""
        if channel is None:
            await self.config.guild(ctx.guild).leadership_channel_id.set(None)
            await self.config.guild(ctx.guild).leadership_message_id.set(None)
            return await ctx.send("Leadership channel cleared.")
        await self.config.guild(ctx.guild).leadership_channel_id.set(channel.id)
        await self.config.guild(ctx.guild).leadership_message_id.set(None)
        await ctx.send(f"Leadership channel set to {channel.mention}. Running `[p]leadership` will post a new message.")

    @leadershipset.command(name="saintrole")
    async def set_saint(self, ctx: commands.Context, role: discord.Role = None):
        """Set (or clear) the Saint role shown at the top of the board."""
        if role is None:
            await self.config.guild(ctx.guild).saint_role_id.set(None)
            return await ctx.send("Saint role cleared.")
        await self.config.guild(ctx.guild).saint_role_id.set(role.id)
        await ctx.send(f"Saint role set to {role.mention}.")

    @leadershipset.command(name="luminariesrole")
    async def set_luminaries(self, ctx: commands.Context, role: discord.Role = None):
        """Set (or clear) the Luminaries role shown at the top of the board."""
        if role is None:
            await self.config.guild(ctx.guild).luminaries_role_id.set(None)
            return await ctx.send("Luminaries role cleared.")
        await self.config.guild(ctx.guild).luminaries_role_id.set(role.id)
        await ctx.send(f"Luminaries role set to {role.mention}.")

    @leadershipset.command(name="addtrigger")
    async def add_trigger(self, ctx: commands.Context, role: discord.Role):
        """Add a role whose changes trigger a board rebuild."""
        async with self.config.guild(ctx.guild).trigger_role_ids() as ids:
            if role.id in ids:
                return await ctx.send(f"{role.mention} is already a trigger.")
            ids.append(role.id)
        await ctx.send(f"Added {role.mention} as a trigger role.")

    @leadershipset.command(name="removetrigger")
    async def remove_trigger(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from the trigger list."""
        async with self.config.guild(ctx.guild).trigger_role_ids() as ids:
            if role.id not in ids:
                return await ctx.send(f"{role.mention} isn't in the trigger list.")
            ids.remove(role.id)
        await ctx.send(f"Removed {role.mention} from trigger roles.")

    @leadershipset.command(name="poll")
    async def set_poll(self, ctx: commands.Context, seconds: int):
        """Set Roblox poll interval in seconds (minimum 60)."""
        seconds = max(60, seconds)
        await self.config.guild(ctx.guild).poll_interval.set(seconds)
        await ctx.send(f"Poll interval set to **{seconds}s**.")

    @leadershipset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current leadership settings."""
        data = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(data["leadership_channel_id"]) if data["leadership_channel_id"] else None
        saint = ctx.guild.get_role(data["saint_role_id"]) if data["saint_role_id"] else None
        lum = ctx.guild.get_role(data["luminaries_role_id"]) if data["luminaries_role_id"] else None
        triggers = []
        for rid in data.get("trigger_role_ids", []):
            r = ctx.guild.get_role(rid)
            triggers.append(r.mention if r else f"`{rid}` (missing)")

        embed = discord.Embed(title="MoonlightLeadership Settings", color=0x5865f2)
        embed.add_field(name="Is main guild", value="Yes" if data.get("is_main_guild") else "No", inline=False)
        embed.add_field(name="Channel", value=channel.mention if channel else "Not set", inline=False)
        embed.add_field(name="Saint role", value=saint.mention if saint else "Not set", inline=False)
        embed.add_field(name="Luminaries role", value=lum.mention if lum else "Not set", inline=False)
        embed.add_field(name="Trigger roles", value="\n".join(triggers) or "None", inline=False)
        embed.add_field(name="Poll interval", value=f"{data['poll_interval']}s", inline=False)
        embed.add_field(name="Sections configured", value=f"{len(SECTIONS)} (hardcoded in source)", inline=False)
        await ctx.send(embed=embed)
