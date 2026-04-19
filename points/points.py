import discord
import asyncpg
import os
from datetime import datetime
from redbot.core import commands, Config
from redbot.core.bot import Red


def footer_text():
    now = datetime.now().strftime("%I:%M %p")
    return f"Children of The Moonlight • Today at {now}"


class MoonlightPoints(commands.Cog):
    """Per-guild points system with leaderboard and logging."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710047, force_registration=True)
        self.config.register_guild(
            log_channel_id=None,
            allowed_role_ids=[],
            thumbnail_url=None,
        )
        self.pool = None

    async def cog_load(self):
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("[points] DATABASE_URL env var not set — cog will not function until set.")
            return
        self.pool = await asyncpg.create_pool(db_url, ssl="require")
        await self._ensure_schema()

    async def cog_unload(self):
        if self.pool:
            await self.pool.close()

    async def _ensure_schema(self):
        """Create the table if missing, and add guild_id column to existing table if needed."""
        async with self.pool.acquire() as conn:
            # Create table with guild_id as part of composite PK
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS moonlight_points (
                    user_id TEXT NOT NULL,
                    username TEXT,
                    points INTEGER DEFAULT 0,
                    guild_id TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY (user_id, guild_id)
                )
            """)
            # Migrate an older single-PK schema if present
            existing_cols = await conn.fetch("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='moonlight_points'
            """)
            col_names = {r["column_name"] for r in existing_cols}
            if "guild_id" not in col_names:
                print("[points] Migrating schema: adding guild_id column.")
                await conn.execute("ALTER TABLE moonlight_points ADD COLUMN guild_id TEXT NOT NULL DEFAULT '0'")
                # Drop old PK if it was only on user_id, add composite PK
                try:
                    await conn.execute("ALTER TABLE moonlight_points DROP CONSTRAINT moonlight_points_pkey")
                except asyncpg.PostgresError:
                    pass
                try:
                    await conn.execute("ALTER TABLE moonlight_points ADD PRIMARY KEY (user_id, guild_id)")
                except asyncpg.PostgresError as e:
                    print(f"[points] Could not create composite PK (may already exist): {e}")

    # ─────────── Permission check ───────────

    async def _user_allowed(self, member: discord.Member) -> bool:
        """Allowed if admin OR has one of the configured allowed roles."""
        if member.guild_permissions.administrator:
            return True
        allowed_ids = await self.config.guild(member.guild).allowed_role_ids()
        return any(r.id in allowed_ids for r in member.roles)

    # ─────────── DB ops ───────────

    async def get_points(self, guild_id: int, user_id: int):
        if not self.pool:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM moonlight_points WHERE user_id=$1 AND guild_id=$2",
            str(user_id), str(guild_id),
        )

    async def upsert_points(self, guild_id: int, user_id: int, username: str, delta: int) -> int:
        """Apply delta to a user's points (floors at 0). Returns new total."""
        if not self.pool:
            raise RuntimeError("DB pool not initialized")
        await self.pool.execute("""
            INSERT INTO moonlight_points (user_id, guild_id, username, points)
            VALUES ($1, $2, $3, GREATEST(0, $4))
            ON CONFLICT (user_id, guild_id)
            DO UPDATE SET
                points = GREATEST(0, moonlight_points.points + $4),
                username = $3
        """, str(user_id), str(guild_id), username, delta)
        row = await self.pool.fetchrow(
            "SELECT points FROM moonlight_points WHERE user_id=$1 AND guild_id=$2",
            str(user_id), str(guild_id),
        )
        return row["points"] if row else 0

    async def get_leaderboard(self, guild_id: int, limit: int = 25):
        if not self.pool:
            return []
        return await self.pool.fetch(
            "SELECT * FROM moonlight_points WHERE guild_id=$1 ORDER BY points DESC LIMIT $2",
            str(guild_id), limit,
        )

    # ─────────── Embeds ───────────

    async def _points_embed(self, guild: discord.Guild, target: discord.abc.User, pts: int):
        thumb = await self.config.guild(guild).thumbnail_url()
        embed = discord.Embed(
            title=f"Points for {target.display_name}",
            description=f"{target.mention}\nPoints: **{pts}**",
            color=0x2b2d31,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        if thumb:
            embed.set_image(url=thumb)
        embed.set_footer(text=footer_text())
        return embed

    async def _log_points(self, guild: discord.Guild, action: str, actor, target, delta: int, new_total: int):
        channel_id = await self.config.guild(guild).log_channel_id()
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        thumb = await self.config.guild(guild).thumbnail_url()
        color = 0x57f287 if action == "added" else 0xed4245
        embed = discord.Embed(
            title="Points Update",
            description=f"Action: **{action}**",
            color=color,
        )
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.add_field(name="Actor", value=f"{actor.mention} (`{actor.id}`)", inline=False)
        embed.add_field(name="Target", value=f"{target.mention} (`{target.id}`)", inline=False)
        sign = "+" if action == "added" else "-"
        embed.add_field(name="Delta", value=f"{sign}{abs(delta)}", inline=True)
        embed.add_field(name="New Total", value=str(new_total), inline=True)
        embed.set_footer(text=footer_text())
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ─────────── Commands (hybrid) ───────────

    @commands.hybrid_group(name="pts", fallback="help")
    @commands.guild_only()
    async def pts(self, ctx: commands.Context):
        """Points commands. Subcommands: add, remove, check, leaderboard."""
        await ctx.send_help(ctx.command)

    @pts.command(name="add", description="Add points to a member")
    async def pts_add(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Add points to a member (requires permission)."""
        if self.pool is None:
            return await ctx.send("❌ Database not available. Is `DATABASE_URL` set?", ephemeral=True)
        if amount <= 0:
            return await ctx.send("Amount must be positive.", ephemeral=True)
        if not await self._user_allowed(ctx.author):
            return await ctx.send("You don't have permission to modify points.", ephemeral=True)

        new_total = await self.upsert_points(ctx.guild.id, member.id, member.name, amount)
        await self._log_points(ctx.guild, "added", ctx.author, member, amount, new_total)
        await ctx.send(f"✅ Added **{amount}** points to {member.mention}. New total: **{new_total}**.")

    @pts.command(name="remove", description="Remove points from a member")
    async def pts_remove(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Remove points from a member (requires permission). Floors at 0."""
        if self.pool is None:
            return await ctx.send("❌ Database not available. Is `DATABASE_URL` set?", ephemeral=True)
        if amount <= 0:
            return await ctx.send("Amount must be positive.", ephemeral=True)
        if not await self._user_allowed(ctx.author):
            return await ctx.send("You don't have permission to modify points.", ephemeral=True)

        new_total = await self.upsert_points(ctx.guild.id, member.id, member.name, -amount)
        await self._log_points(ctx.guild, "removed", ctx.author, member, amount, new_total)
        await ctx.send(f"✅ Removed **{amount}** points from {member.mention}. New total: **{new_total}**.")

    @pts.command(name="check", description="Check a member's points")
    async def pts_check(self, ctx: commands.Context, member: discord.Member = None):
        """Check a member's points. Defaults to yourself."""
        if self.pool is None:
            return await ctx.send("❌ Database not available. Is `DATABASE_URL` set?", ephemeral=True)
        target = member or ctx.author
        row = await self.get_points(ctx.guild.id, target.id)
        pts = row["points"] if row else 0
        await ctx.send(embed=await self._points_embed(ctx.guild, target, pts))

    @pts.command(name="leaderboard", aliases=["checkall"], description="Show the top 25 points leaderboard")
    async def pts_leaderboard(self, ctx: commands.Context):
        """Show the top 25 points leaderboard for this server."""
        if self.pool is None:
            return await ctx.send("❌ Database not available. Is `DATABASE_URL` set?", ephemeral=True)
        rows = await self.get_leaderboard(ctx.guild.id, 25)
        if not rows:
            return await ctx.send("No points recorded yet in this server.")

        lines = []
        for i, r in enumerate(rows, start=1):
            lines.append(f"**{i}.** <@{r['user_id']}> — **{r['points']}**")
        embed = discord.Embed(
            title="🌙 Points Leaderboard",
            description="\n".join(lines),
            color=0x2b2d31,
        )
        thumb = await self.config.guild(ctx.guild).thumbnail_url()
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text=footer_text())
        await ctx.send(embed=embed)

    # ─────────── Admin: pointsset ───────────

    @commands.group(name="pointsset")
    @commands.admin_or_permissions(manage_guild=True)
    async def pointsset(self, ctx: commands.Context):
        """Configure MoonlightPoints for this server."""

    @pointsset.command(name="logchannel")
    async def set_log(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the channel where points changes are logged."""
        if channel is None:
            await self.config.guild(ctx.guild).log_channel_id.set(None)
            return await ctx.send("Points log channel cleared.")
        await self.config.guild(ctx.guild).log_channel_id.set(channel.id)
        await ctx.send(f"Points log channel set to {channel.mention}.")

    @pointsset.command(name="allowrole")
    async def add_allowed(self, ctx: commands.Context, role: discord.Role):
        """Add a role that can add/remove points."""
        async with self.config.guild(ctx.guild).allowed_role_ids() as ids:
            if role.id in ids:
                return await ctx.send(f"{role.mention} is already allowed.")
            ids.append(role.id)
        await ctx.send(f"Added {role.mention} to allowed roles.")

    @pointsset.command(name="denyrole")
    async def remove_allowed(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from the allowed list."""
        async with self.config.guild(ctx.guild).allowed_role_ids() as ids:
            if role.id not in ids:
                return await ctx.send(f"{role.mention} isn't in the allowed list.")
            ids.remove(role.id)
        await ctx.send(f"Removed {role.mention} from allowed roles.")

    @pointsset.command(name="thumbnail")
    async def set_thumb(self, ctx: commands.Context, url: str = None):
        """Set (or clear) the thumbnail image URL used in points embeds."""
        if url is None:
            await self.config.guild(ctx.guild).thumbnail_url.set(None)
            return await ctx.send("Points thumbnail cleared.")
        await self.config.guild(ctx.guild).thumbnail_url.set(url)
        await ctx.send(f"Points thumbnail set.")

    @pointsset.command(name="reset")
    async def reset_user(self, ctx: commands.Context, member: discord.Member):
        """Reset a specific member's points to 0 in this server."""
        if self.pool is None:
            return await ctx.send("❌ Database not available.")
        await self.pool.execute(
            "DELETE FROM moonlight_points WHERE user_id=$1 AND guild_id=$2",
            str(member.id), str(ctx.guild.id),
        )
        await ctx.send(f"Reset points for {member.mention}.")

    @pointsset.command(name="resetall")
    async def reset_all(self, ctx: commands.Context, confirm: str = ""):
        """DANGER: Wipe all points for this server. Pass 'yes' to confirm."""
        if confirm.lower() != "yes":
            return await ctx.send(
                "This will delete **all** points data for this server. "
                f"Run `{ctx.prefix}pointsset resetall yes` to confirm."
            )
        if self.pool is None:
            return await ctx.send("❌ Database not available.")
        await self.pool.execute(
            "DELETE FROM moonlight_points WHERE guild_id=$1",
            str(ctx.guild.id),
        )
        await ctx.send("✅ All points for this server have been wiped.")

    @pointsset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current settings for this server."""
        data = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(data["log_channel_id"]) if data["log_channel_id"] else None
        roles = []
        for rid in data.get("allowed_role_ids", []):
            r = ctx.guild.get_role(rid)
            roles.append(r.mention if r else f"`{rid}` (missing)")

        db_status = "✅ Connected" if self.pool else "❌ Not connected (DATABASE_URL missing)"

        embed = discord.Embed(title="MoonlightPoints Settings", color=0x5865f2)
        embed.add_field(name="Database", value=db_status, inline=False)
        embed.add_field(name="Log channel", value=ch.mention if ch else "Not set", inline=False)
        embed.add_field(name="Allowed roles", value="\n".join(roles) or "None (admins only)", inline=False)
        embed.add_field(name="Thumbnail", value="Set" if data.get("thumbnail_url") else "Not set", inline=False)
        await ctx.send(embed=embed)
