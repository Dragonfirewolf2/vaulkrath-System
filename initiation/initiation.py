import discord
import asyncio
from datetime import datetime
from redbot.core import commands, Config
from redbot.core.bot import Red


def footer_text():
    now = datetime.now().strftime("%I:%M %p")
    return f"Children of The Moonlight • Today at {now}"


# Tracks pending ritual forms: event_id -> event data
pending_rituals = {}


class RitualModal(discord.ui.Modal, title="Ritual Form"):
    initiated = discord.ui.TextInput(
        label="Initiated Member IDs (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="123456789012345678\n987654321098765432",
        required=True,
    )

    def __init__(self, cog, event_id):
        super().__init__()
        self.cog = cog
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        event = pending_rituals.pop(self.event_id, None)
        if not event:
            return await interaction.response.send_message("Ritual form expired.", ephemeral=True)

        raw = self.initiated.value
        user_ids = [
            uid.strip()
            for uid in raw.replace(",", "\n").split("\n")
            if uid.strip().isdigit() and 15 <= len(uid.strip()) <= 20
        ]

        if not user_ids:
            return await interaction.response.send_message(
                "No valid user IDs found in your submission.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        await self.cog.process_ritual(interaction, event, user_ids)
        await interaction.followup.send(f"Done. Processed {len(user_ids)} member(s).", ephemeral=True)


class RitualButton(discord.ui.View):
    def __init__(self, cog, event_id, host_id):
        super().__init__(timeout=180)
        self.cog = cog
        self.event_id = event_id
        self.host_id = host_id

    @discord.ui.button(label="Open Initiation Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        event = pending_rituals.get(self.event_id)
        if not event:
            return await interaction.response.send_message("This form has expired.", ephemeral=True)

        # Only host or someone with allowed role may submit
        allowed = False
        if interaction.user.id == self.host_id:
            allowed = True
        else:
            allowed_ids = await self.cog.config.guild(interaction.guild).allowed_role_ids()
            if any(r.id in allowed_ids for r in interaction.user.roles):
                allowed = True
        if not allowed:
            return await interaction.response.send_message("Only the host can submit.", ephemeral=True)

        await interaction.response.send_modal(RitualModal(self.cog, self.event_id))

    async def on_timeout(self):
        pending_rituals.pop(self.event_id, None)
        for item in self.children:
            item.disabled = True


class MoonlightRitual(commands.Cog):
    """Log initiation rituals and auto-rank initiates to the newborn rank."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710043, force_registration=True)
        self.config.register_global(
            global_ritual_log_channel_id=None,
            global_rank_announcement_channel_id=None,
        )
        self.config.register_guild(
            ritual_log_channel_id=None,
            rank_announcement_channel_id=None,
            allowed_role_ids=[],  # Roles allowed to run /ritual
            newborn_rank_name="Newborn",
        )

    # ─────────── Channel resolution ───────────

    async def _get_log_channel(self, guild: discord.Guild):
        """Return the ritual log channel: per-guild if set, else global fallback."""
        local_id = await self.config.guild(guild).ritual_log_channel_id()
        if local_id:
            ch = guild.get_channel(local_id)
            if ch:
                return ch
        global_id = await self.config.global_ritual_log_channel_id()
        if global_id:
            return self.bot.get_channel(global_id)
        return None

    async def _get_rank_channel(self, guild: discord.Guild):
        """Return the rank announcement channel: per-guild if set, else global fallback."""
        local_id = await self.config.guild(guild).rank_announcement_channel_id()
        if local_id:
            ch = guild.get_channel(local_id)
            if ch:
                return ch
        global_id = await self.config.global_rank_announcement_channel_id()
        if global_id:
            return self.bot.get_channel(global_id)
        return None

    # ─────────── Helpers ───────────

    def _get_verify_cog(self):
        return self.bot.get_cog("RobloxVerify")

    async def _user_allowed(self, member: discord.Member) -> bool:
        """Check if a member has an allowed role or is an admin."""
        if member.guild_permissions.administrator:
            return True
        allowed_ids = await self.config.guild(member.guild).allowed_role_ids()
        return any(r.id in allowed_ids for r in member.roles)

    # ─────────── Core ritual processing ───────────

    async def process_ritual(self, interaction: discord.Interaction, event: dict, user_ids: list):
        """Post log embed + rank each initiate to newborn across all configured guilds."""
        guild = interaction.guild
        cfg = await self.config.guild(guild).all()
        verify_cog = self._get_verify_cog()

        now = datetime.now()
        initiated_list = "\n".join(f"<@{uid}> — (`{uid}`)" for uid in user_ids) or "*None*"

        # ── Log embed ──
        log_embed = discord.Embed(
            title="Initiation Complete",
            description=(
                f"The initiation ordered by <@{event['host_id']}> concluded on "
                f"<t:{int(now.timestamp())}:F>."
            ),
            color=0x2b2d31,
        )
        log_embed.add_field(name="Server", value=guild.name, inline=False)
        log_embed.add_field(name="Proof", value="See attached image.", inline=False)
        if event.get("witnesses"):
            log_embed.add_field(
                name="Witnesses",
                value=", ".join(f"<@{wid}>" for wid in event["witnesses"]),
                inline=False,
            )
        if event.get("assistants"):
            log_embed.add_field(
                name="Assistants",
                value=", ".join(f"<@{aid}>" for aid in event["assistants"]),
                inline=False,
            )
        if event.get("image_url"):
            log_embed.set_image(url=event["image_url"])
        log_embed.set_footer(text=footer_text())

        members_embed = discord.Embed(
            title="Initiated Members",
            description=f"These members have been initiated.\n\n{initiated_list[:2048]}",
            color=0xed4245,
        )
        members_embed.set_footer(text=f"Total: {len(user_ids)} member(s)")

        log_channel = await self._get_log_channel(guild)
        if log_channel:
            try:
                await log_channel.send(embeds=[log_embed, members_embed])
            except discord.HTTPException:
                pass

        # ── Rank each initiate to Newborn ──
        rank_channel = await self._get_rank_channel(guild)
        newborn_rank = cfg.get("newborn_rank_name", "Newborn")

        for uid in user_ids:
            await self._rank_to_newborn(
                guild=guild,
                discord_id=int(uid),
                rank_channel=rank_channel,
                verify_cog=verify_cog,
                newborn_rank=newborn_rank,
            )

    async def _rank_to_newborn(self, guild, discord_id, rank_channel, verify_cog, newborn_rank):
        """Rank one member to newborn — post embed, try ranking, react with result."""
        try:
            if verify_cog is None:
                if rank_channel:
                    await self._post_rank_error(rank_channel, discord_id, "RobloxVerify cog not loaded.")
                return

            data = await verify_cog.get_verified_data(discord_id)
            if not data:
                if rank_channel:
                    embed = discord.Embed(
                        description=f"<@{discord_id}> | `{discord_id}`\nHas been reborn and must be ranked (not verified).",
                        color=0xed4245,
                    )
                    embed.set_footer(text=footer_text())
                    msg = await rank_channel.send(embed=embed)
                    await msg.add_reaction("🔴")
                    await asyncio.sleep(300)
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
                return

            roblox_id = int(data["roblox_id"])
            roblox_username = data["roblox_username"]

            rank_msg = None
            if rank_channel:
                embed = discord.Embed(
                    description=(
                        f"<@{discord_id}> | {roblox_username} | `{discord_id}`\n"
                        f"Has been reborn and must be ranked."
                    ),
                    color=0x2b2d31,
                )
                embed.set_footer(text=footer_text())
                rank_msg = await rank_channel.send(embed=embed)

            # Attempt Roblox rank change
            ok, msg = await verify_cog.rank_on_roblox(roblox_id, newborn_rank, guild=guild)

            if ok:
                # Update saved rank, sync Discord roles/nick everywhere
                await verify_cog.save_verified(discord_id, roblox_id, roblox_username, newborn_rank)
                await verify_cog.apply_roles_everywhere(discord_id, roblox_username, newborn_rank)

            if rank_msg:
                await rank_msg.add_reaction("🟢" if ok else "🔴")
                await asyncio.sleep(30 if ok else 300)
                try:
                    await rank_msg.delete()
                except discord.HTTPException:
                    pass

        except Exception as e:
            if rank_channel:
                await self._post_rank_error(rank_channel, discord_id, str(e))

    async def _post_rank_error(self, channel, discord_id, error):
        embed = discord.Embed(
            description=f"🔴 Error ranking <@{discord_id}>: `{error}`",
            color=0xed4245,
        )
        embed.set_footer(text=footer_text())
        try:
            msg = await channel.send(embed=embed)
            await msg.add_reaction("🔴")
            await asyncio.sleep(300)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            pass

    # ─────────── Commands ───────────

    @commands.hybrid_command(name="ritual", description="Log an initiation ritual")
    async def ritual(
        self,
        ctx: commands.Context,
        image: discord.Attachment,
        witness1: discord.Member = None,
        witness2: discord.Member = None,
        witness3: discord.Member = None,
        witness4: discord.Member = None,
        witness5: discord.Member = None,
        assistant1: discord.Member = None,
        assistant2: discord.Member = None,
        assistant3: discord.Member = None,
        assistant4: discord.Member = None,
        assistant5: discord.Member = None,
    ):
        """Log an initiation ritual with proof image and optional witnesses/assistants."""
        if ctx.guild is None:
            return await ctx.send("Run this in a server.")
        if not await self._user_allowed(ctx.author):
            return await ctx.send("You don't have permission to run rituals.", ephemeral=True)

        # Sanity check: RobloxVerify must be loaded
        if self._get_verify_cog() is None:
            return await ctx.send(
                "❌ The RobloxVerify cog is not loaded. Load it first with `[p]load robloxverify`.",
                ephemeral=True,
            )

        witnesses = [w.id for w in (witness1, witness2, witness3, witness4, witness5) if w]
        assistants = [a.id for a in (assistant1, assistant2, assistant3, assistant4, assistant5) if a]

        event_id = f"ritual_{ctx.message.id if ctx.message else ctx.interaction.id}"
        pending_rituals[event_id] = {
            "host_id": ctx.author.id,
            "image_url": image.url,
            "witnesses": witnesses,
            "assistants": assistants,
        }

        content = (
            "Initiation form ready. Click the button below to open it. "
            "This button expires in 3 minutes.\n\n"
            "Have the initiated user(s)' IDs ready."
        )
        view = RitualButton(self, event_id, ctx.author.id)
        await ctx.send(content=content, view=view)

    # ─────────── Admin: ritualset ───────────

    @commands.group(name="ritualset")
    @commands.admin_or_permissions(manage_guild=True)
    async def ritualset(self, ctx: commands.Context):
        """Configure MoonlightRitual for this server."""

    @ritualset.command(name="logchannel")
    async def set_log_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the ritual log channel."""
        if channel is None:
            await self.config.guild(ctx.guild).ritual_log_channel_id.set(None)
            return await ctx.send("Ritual log channel cleared.")
        await self.config.guild(ctx.guild).ritual_log_channel_id.set(channel.id)
        await ctx.send(f"Ritual log channel set to {channel.mention}.")

    @ritualset.command(name="rankchannel")
    async def set_rank_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the rank announcement channel."""
        if channel is None:
            await self.config.guild(ctx.guild).rank_announcement_channel_id.set(None)
            return await ctx.send("Rank announcement channel cleared.")
        await self.config.guild(ctx.guild).rank_announcement_channel_id.set(channel.id)
        await ctx.send(f"Rank announcement channel set to {channel.mention}.")

    @ritualset.command(name="allowrole")
    async def add_allowed_role(self, ctx: commands.Context, role: discord.Role):
        """Add a role that's allowed to run rituals."""
        async with self.config.guild(ctx.guild).allowed_role_ids() as ids:
            if role.id in ids:
                return await ctx.send(f"{role.mention} is already allowed.")
            ids.append(role.id)
        await ctx.send(f"Added {role.mention} to allowed roles.")

    @ritualset.command(name="denyrole")
    async def remove_allowed_role(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from those allowed to run rituals."""
        async with self.config.guild(ctx.guild).allowed_role_ids() as ids:
            if role.id not in ids:
                return await ctx.send(f"{role.mention} isn't in the allowed list.")
            ids.remove(role.id)
        await ctx.send(f"Removed {role.mention} from allowed roles.")

    @ritualset.command(name="newbornrank")
    async def set_newborn_rank(self, ctx: commands.Context, *, rank_name: str):
        """Set the Roblox rank name assigned to initiates (default: Newborn)."""
        await self.config.guild(ctx.guild).newborn_rank_name.set(rank_name.strip())
        await ctx.send(f"Newborn rank name set to `{rank_name.strip()}`.")

    @ritualset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current ritual settings."""
        data = await self.config.guild(ctx.guild).all()
        log_ch = ctx.guild.get_channel(data["ritual_log_channel_id"]) if data["ritual_log_channel_id"] else None
        rank_ch = ctx.guild.get_channel(data["rank_announcement_channel_id"]) if data["rank_announcement_channel_id"] else None
        allowed_roles = []
        for rid in data.get("allowed_role_ids", []):
            r = ctx.guild.get_role(rid)
            allowed_roles.append(r.mention if r else f"`{rid}` (missing)")

        # Global fallbacks
        global_log_id = await self.config.global_ritual_log_channel_id()
        global_rank_id = await self.config.global_rank_announcement_channel_id()
        global_log_ch = self.bot.get_channel(global_log_id) if global_log_id else None
        global_rank_ch = self.bot.get_channel(global_rank_id) if global_rank_id else None

        def _format_global(ch, gid):
            if ch:
                return f"{ch.mention} in **{ch.guild.name}**"
            if gid:
                return f"`{gid}` (not accessible)"
            return "Not set"

        embed = discord.Embed(title="MoonlightRitual Settings", color=0x5865f2)
        embed.add_field(name="This server's log channel", value=log_ch.mention if log_ch else "Not set", inline=False)
        embed.add_field(name="Global log channel (fallback)", value=_format_global(global_log_ch, global_log_id), inline=False)
        embed.add_field(name="This server's rank channel", value=rank_ch.mention if rank_ch else "Not set", inline=False)
        embed.add_field(name="Global rank channel (fallback)", value=_format_global(global_rank_ch, global_rank_id), inline=False)
        embed.add_field(name="Allowed roles", value="\n".join(allowed_roles) or "None (admins only)", inline=False)
        embed.add_field(name="Newborn rank", value=f"`{data['newborn_rank_name']}`", inline=False)
        await ctx.send(embed=embed)

    # ─────────── Bot-owner: cross-guild global channels ───────────

    @ritualset.command(name="globallog")
    @commands.is_owner()
    async def set_global_log(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """[Bot owner] Set a channel that receives ritual logs from ALL guilds.

        Per-guild log channels (set with `logchannel`) take priority.
        Pass no channel to clear.
        """
        if channel is None:
            await self.config.global_ritual_log_channel_id.set(None)
            return await ctx.send("Global ritual log channel cleared.")
        await self.config.global_ritual_log_channel_id.set(channel.id)
        await ctx.send(
            f"Global ritual log set to {channel.mention} in **{channel.guild.name}**.\n"
            f"Guilds without a per-guild log will log here."
        )

    @ritualset.command(name="globalrank")
    @commands.is_owner()
    async def set_global_rank(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """[Bot owner] Set a channel that receives rank announcements from ALL guilds.

        Per-guild rank channels (set with `rankchannel`) take priority.
        Pass no channel to clear.
        """
        if channel is None:
            await self.config.global_rank_announcement_channel_id.set(None)
            return await ctx.send("Global rank announcement channel cleared.")
        await self.config.global_rank_announcement_channel_id.set(channel.id)
        await ctx.send(
            f"Global rank channel set to {channel.mention} in **{channel.guild.name}**.\n"
            f"Guilds without a per-guild rank channel will post here."
        )
