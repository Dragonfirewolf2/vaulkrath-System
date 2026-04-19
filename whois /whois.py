import discord
import aiohttp
import asyncio
from redbot.core import commands, Config
from redbot.core.bot import Red


# ─────────── API helpers ───────────

async def fetch_json(url, headers=None):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url, headers=headers or {}) as r:
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
    except Exception:
        return None


async def get_roblox_user(roblox_id):
    return await fetch_json(f"https://users.roblox.com/v1/users/{roblox_id}")


async def get_roblox_avatar(roblox_id):
    data = await fetch_json(
        f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={roblox_id}&size=150x150&format=Png"
    )
    if data and data.get("data"):
        return data["data"][0].get("imageUrl")
    return None


async def get_all_group_ranks(roblox_id, tracked_groups: dict):
    """
    tracked_groups: {display_name: group_id}
    Returns {display_name: "RankName (N)"} for groups the user is in.
    """
    data = await fetch_json(f"https://groups.roblox.com/v1/users/{roblox_id}/groups/roles")
    if not data:
        return {}
    joined = {g["group"]["id"]: g for g in data.get("data", [])}
    result = {}
    for name, group_id in tracked_groups.items():
        if group_id in joined:
            role = joined[group_id]["role"]
            result[name] = f"{role['name']} ({role['rank']})"
    return result


async def get_rotector(roblox_id):
    try:
        return await fetch_json(f"https://api.rotector.com/v1/users/{roblox_id}")
    except Exception:
        return None


async def get_discord_banner(member: discord.Member):
    """Fetch banner via API since gateway doesn't always include it."""
    try:
        user = await member._state.http.get_user(member.id)
        banner_hash = user.get("banner")
        if banner_hash:
            fmt = "gif" if banner_hash.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/banners/{member.id}/{banner_hash}.{fmt}?size=512"
    except Exception:
        pass
    return None


# ─────────── Embed builders ───────────

def _footer_text(requester):
    return f"Requested by {requester.name}"


def build_discord_embed(member: discord.Member, requester, banner_url=None):
    created_ts = int(member.created_at.timestamp())
    joined_ts = int(member.joined_at.timestamp()) if member.joined_at else None

    roles = [r for r in reversed(member.roles) if r.id != member.guild.id]
    top_roles = roles[:8]
    role_text = ", ".join(r.mention for r in top_roles)
    if len(roles) > 8:
        role_text += f", +{len(roles) - 8} more"
    highest_role = roles[0] if roles else None

    status_map = {
        discord.Status.online: "Online",
        discord.Status.idle: "Idle",
        discord.Status.dnd: "DND",
        discord.Status.offline: "Offline",
    }
    status = status_map.get(member.status, "Unknown")
    if member.mobile_status != discord.Status.offline:
        status += " • Mobile"
    elif member.web_status != discord.Status.offline:
        status += " • Web"

    embed = discord.Embed(title=f"User Profile: {member.display_name}", color=0x2b2d31)
    embed.set_thumbnail(url=member.display_avatar.url)
    if banner_url:
        embed.set_image(url=banner_url)

    embed.add_field(name="Display Name", value=member.display_name, inline=True)
    embed.add_field(name="Username", value=member.name, inline=True)
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    embed.add_field(name="Mention", value=member.mention, inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Account Created", value=f"<t:{created_ts}:F>\n<t:{created_ts}:R>", inline=False)
    if joined_ts:
        embed.add_field(name="Joined Server", value=f"<t:{joined_ts}:F>\n<t:{joined_ts}:R>", inline=False)
    embed.add_field(name="Highest Role", value=highest_role.mention if highest_role else "None", inline=True)
    embed.add_field(name="Role Count", value=str(len(roles)), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="Top Roles", value=role_text or "None", inline=False)
    embed.set_footer(text=_footer_text(requester))
    return embed


def build_roblox_embed(member, verified_data, group_ranks, requester, avatar_url):
    roblox_id = verified_data.get("roblox_id", "?")
    roblox_name = verified_data.get("roblox_username", "Unknown")
    rank_name = verified_data.get("rank_name")
    verified_at = verified_data.get("verified_at")

    embed = discord.Embed(
        title="Roblox Account",
        description=f"[Profile on Roblox](https://www.roblox.com/users/{roblox_id}/profile)",
        color=0x2b2d31,
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="Discord", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Roblox ID", value=str(roblox_id), inline=True)
    embed.add_field(name="Roblox Name", value=roblox_name, inline=True)

    if rank_name:
        embed.add_field(name="Main Rank", value=rank_name, inline=True)
    if verified_at:
        embed.add_field(name="Last Verified", value=verified_at[:10], inline=True)

    if group_ranks:
        ranks_text = "\n".join(f"**{name}** — {rank}" for name, rank in group_ranks.items())
        embed.add_field(name="Group Ranks", value=ranks_text[:1024], inline=False)
    else:
        embed.add_field(name="Group Ranks", value="Not in any tracked groups.", inline=False)

    embed.set_footer(text=_footer_text(requester))
    return embed


def build_rotector_embed(member, verified_data, rotector_data, requester, avatar_url):
    roblox_id = verified_data.get("roblox_id", "?")
    embed = discord.Embed(
        title="RoTector Safety Check",
        description="Live lookup from [RoTector](https://rotector.com).",
        color=0x2b2d31,
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="Discord", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Roblox ID", value=str(roblox_id), inline=True)

    if rotector_data:
        flag_type = rotector_data.get("flagType", 0)
        flag_str = "🔴 Flagged" if flag_type > 0 else "🟢 Unflagged"
        embed.add_field(name="Flag Type", value=f"{flag_str} ({flag_type})", inline=True)
        embed.add_field(name="Actionable", value="Yes" if rotector_data.get("actionable") else "No", inline=True)
        embed.add_field(name="Confidence", value=str(rotector_data.get("confidence", "—")), inline=True)
        embed.add_field(name="Reportable", value=str(rotector_data.get("reportable", "—")), inline=True)
        embed.add_field(name="Locked", value=str(rotector_data.get("locked", "—")), inline=True)
        embed.add_field(name="Processed", value=str(rotector_data.get("processed", "—")), inline=True)
    else:
        embed.add_field(name="Status", value="⚠️ RoTector unavailable (no data returned).", inline=False)

    embed.add_field(
        name="Notice",
        value="Only flag types 1 and 2 are auto-actionable.\nThis is a live lookup — not cached.\nAppeals: https://rotector.com",
        inline=False,
    )
    embed.set_footer(text=_footer_text(requester))
    return embed


# ─────────── View ───────────

class ProfileView(discord.ui.View):
    def __init__(self, target, requester, verified_data, group_ranks, rotector_data,
                 avatar_url, banner_url, has_roblox, use_rotector):
        super().__init__(timeout=300)
        self.target = target
        self.requester = requester
        self.verified_data = verified_data
        self.group_ranks = group_ranks
        self.rotector_data = rotector_data
        self.avatar_url = avatar_url
        self.banner_url = banner_url
        self.current = "discord"

        # Disable buttons when no Roblox data linked
        if not has_roblox:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label in ("Roblox", "RoTector"):
                    item.disabled = True
        elif not use_rotector:
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "RoTector":
                    item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("This isn't your profile view.", ephemeral=True)
            return False
        return True

    def current_embed(self):
        if self.current == "discord":
            return build_discord_embed(self.target, self.requester, self.banner_url)
        if self.current == "roblox":
            return build_roblox_embed(self.target, self.verified_data, self.group_ranks, self.requester, self.avatar_url)
        if self.current == "rotector":
            return build_rotector_embed(self.target, self.verified_data, self.rotector_data, self.requester, self.avatar_url)

    @discord.ui.button(label="Discord", style=discord.ButtonStyle.secondary, emoji="💬")
    async def discord_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = "discord"
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Roblox", style=discord.ButtonStyle.primary, emoji="🎮")
    async def roblox_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = "roblox"
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="RoTector", style=discord.ButtonStyle.danger, emoji="🛡️")
    async def rotector_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = "rotector"
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


# ─────────── Cog ───────────

class MoonlightWhois(commands.Cog):
    """Rich profile view with Discord / Roblox / RoTector tabs."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710044, force_registration=True)
        self.config.register_guild(
            tracked_groups={},   # dict: display_name -> group_id
            use_rotector=True,
        )

    def _get_verify_cog(self):
        return self.bot.get_cog("RobloxVerify")

    async def _build_profile(self, ctx_or_interaction, target: discord.Member, ephemeral=False):
        """Shared logic for /whoami, /profile, and prefix variants."""
        is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
        requester = ctx_or_interaction.user if is_interaction else ctx_or_interaction.author
        guild = ctx_or_interaction.guild

        verify_cog = self._get_verify_cog()
        verified_data = None
        if verify_cog:
            verified_data = await verify_cog.get_verified_data(target.id)

        avatar_url = None
        group_ranks = {}
        rotector_data = None

        if verified_data and verified_data.get("roblox_id"):
            roblox_id = int(verified_data["roblox_id"])
            cfg = await self.config.guild(guild).all()
            tracked = cfg.get("tracked_groups") or {}
            use_rotector = cfg.get("use_rotector", True)

            # Parallel fetches
            tasks = [
                get_roblox_avatar(roblox_id),
                get_all_group_ranks(roblox_id, tracked) if tracked else asyncio.sleep(0, result={}),
                get_rotector(roblox_id) if use_rotector else asyncio.sleep(0, result=None),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            avatar_url = results[0] if not isinstance(results[0], Exception) else None
            group_ranks = results[1] if not isinstance(results[1], Exception) else {}
            rotector_data = results[2] if not isinstance(results[2], Exception) else None
        else:
            use_rotector = False

        banner_url = await get_discord_banner(target)
        has_roblox = verified_data is not None and bool(verified_data.get("roblox_id"))

        embed = build_discord_embed(target, requester, banner_url)
        view = ProfileView(
            target=target,
            requester=requester,
            verified_data=verified_data or {},
            group_ranks=group_ranks,
            rotector_data=rotector_data,
            avatar_url=avatar_url,
            banner_url=banner_url,
            has_roblox=has_roblox,
            use_rotector=use_rotector,
        )

        if is_interaction:
            if ctx_or_interaction.response.is_done():
                await ctx_or_interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
            else:
                await ctx_or_interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        else:
            await ctx_or_interaction.reply(embed=embed, view=view)

    # ─────────── Commands ───────────

    @commands.hybrid_command(name="whoami", description="View your own profile")
    async def whoami(self, ctx: commands.Context):
        """Show your own profile with Discord/Roblox/RoTector tabs."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)
        await self._build_profile(ctx.interaction or ctx, ctx.author, ephemeral=True)

    @commands.hybrid_command(name="profile", description="View a member's profile")
    async def profile(self, ctx: commands.Context, member: discord.Member = None):
        """Show a member's profile with Discord/Roblox/RoTector tabs."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        target = member or ctx.author
        if ctx.interaction:
            await ctx.interaction.response.defer()
        await self._build_profile(ctx.interaction or ctx, target, ephemeral=False)

    # ─────────── Admin: whoisset ───────────

    @commands.group(name="whoisset")
    @commands.admin_or_permissions(manage_guild=True)
    async def whoisset(self, ctx: commands.Context):
        """Configure MoonlightWhois for this server."""

    @whoisset.group(name="group")
    async def whoisset_group(self, ctx: commands.Context):
        """Manage tracked Roblox groups (for the Roblox tab's rank list)."""

    @whoisset_group.command(name="add")
    async def group_add(self, ctx: commands.Context, group_id: int, *, display_name: str):
        """Add a Roblox group to track. Example: `[p]whoisset group add 575312081 Children of The Moonlight`"""
        async with self.config.guild(ctx.guild).tracked_groups() as groups:
            groups[display_name] = group_id
        await ctx.send(f"✅ Tracking group **{display_name}** (`{group_id}`).")

    @whoisset_group.command(name="remove")
    async def group_remove(self, ctx: commands.Context, *, display_name: str):
        """Remove a tracked group by display name."""
        async with self.config.guild(ctx.guild).tracked_groups() as groups:
            if display_name not in groups:
                return await ctx.send(f"No group named `{display_name}` is tracked.")
            del groups[display_name]
        await ctx.send(f"Removed **{display_name}** from tracked groups.")

    @whoisset_group.command(name="list")
    async def group_list(self, ctx: commands.Context):
        """List tracked groups."""
        groups = await self.config.guild(ctx.guild).tracked_groups()
        if not groups:
            return await ctx.send("No groups tracked. Use `[p]whoisset group add <id> <name>` to add some.")
        lines = [f"• **{name}** — `{gid}`" for name, gid in groups.items()]
        await ctx.send("Tracked groups:\n" + "\n".join(lines))

    @whoisset.command(name="rotector")
    async def set_rotector(self, ctx: commands.Context, on_off: bool):
        """Enable or disable the RoTector tab."""
        await self.config.guild(ctx.guild).use_rotector.set(on_off)
        await ctx.send(f"RoTector tab: **{'on' if on_off else 'off'}**.")

    @whoisset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show MoonlightWhois settings for this server."""
        data = await self.config.guild(ctx.guild).all()
        tracked = data.get("tracked_groups") or {}
        groups_text = "\n".join(f"• {name} (`{gid}`)" for name, gid in tracked.items()) or "None"

        embed = discord.Embed(title="MoonlightWhois Settings", color=0x5865f2)
        embed.add_field(name="RoTector tab", value="On" if data.get("use_rotector", True) else "Off", inline=False)
        embed.add_field(name="Tracked groups", value=groups_text, inline=False)
        await ctx.send(embed=embed)
