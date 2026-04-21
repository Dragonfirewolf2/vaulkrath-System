import discord
import aiohttp
import asyncio
import os
from datetime import datetime, timezone
from redbot.core import commands, Config
from redbot.core.bot import Red


# ─────────── Hardcoded group list ───────────
# Edit this dict to change which Roblox groups to monitor.
MONITORED_GROUPS = {
    575312081:  "Children of The Moonlight",
    689853179:  "The Wulfgard",
    479414319:  "The Ravager",
    201749000:  "The Eviscerater",
    988037427:  "The Drifting Seeker",
    261400171:  "The Sanctum",
    341490152:  "The Halvari",
    693899217:  "The Athenaeum",
    128380705:  "The Verdikari",
}

ACTION_LABELS = {
    "Assign Role":           "Rank Assigned",
    "Unassign Role":         "Rank Removed",
    "Remove Member":         "Member Kicked",
    "Ban User":              "Member Banned",
    "Unban User":            "Member Unbanned",
    "Accept Join Request":   "Join Request Accepted",
    "Decline Join Request":  "Join Request Declined",
    "Delete Role Set":       "Role Deleted",
    "Create Role Set":       "Role Created",
    "Change Rank":           "Rank Changed",
    "Invite To Clan":        "Invited",
    "Lock":                  "Group Locked",
    "Unlock":                "Group Unlocked",
    "Post Status":           "Status Posted",
}

ACTION_COLORS = {
    "Assign Role":           0x5865f2,
    "Unassign Role":         0x5865f2,
    "Remove Member":         0xed4245,
    "Ban User":              0x992d22,
    "Unban User":            0x57f287,
    "Accept Join Request":   0x57f287,
    "Decline Join Request":  0xed4245,
    "Change Rank":           0x5865f2,
}

# Roblox's official "legacy-groups" endpoint that supports API-key auth.
# As of Nov 2025 this is the only way to read audit logs programmatically.
AUDIT_URL = "https://apis.roblox.com/legacy-groups/v1/groups/{group_id}/audit-log?limit=10&sortOrder=Desc"


# ─────────── Helpers ───────────

def footer_text():
    now = datetime.now().strftime("%I:%M %p")
    return f"Children of The Moonlight • Today at {now}"


async def fetch_audit_log(group_id, api_key):
    """Fetch the latest audit log entries for a group. Returns list of entries (newest first) or None."""
    if not api_key:
        return None
    headers = {"x-api-key": api_key}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(AUDIT_URL.format(group_id=group_id), headers=headers) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return data.get("data", [])
                # 401/403 — bad key or missing perms. 429 — rate limited. 5xx — Roblox down.
                body = await r.text()
                print(f"[auditlog] group {group_id} HTTP {r.status}: {body[:160]}")
                return None
    except asyncio.TimeoutError:
        print(f"[auditlog] timeout fetching group {group_id}")
        return None
    except Exception as e:
        print(f"[auditlog] error fetching group {group_id}: {e}")
        return None


async def get_roblox_username(roblox_id):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"https://users.roblox.com/v1/users/{roblox_id}") as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return data.get("name", str(roblox_id))
    except Exception:
        pass
    return str(roblox_id)


def parse_ts(ts_str):
    """Parse Roblox audit log 'created' timestamp to unix int, or None."""
    if not ts_str:
        return None
    try:
        # Handle both "2024-01-01T00:00:00Z" and "2024-01-01T00:00:00.123Z"
        cleaned = ts_str.replace("Z", "+00:00")
        return int(datetime.fromisoformat(cleaned).timestamp())
    except Exception:
        pass
    try:
        return int(datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def match_action(action_str):
    """Return the ACTION_LABELS key matching `action_str`, or None."""
    if action_str in ACTION_LABELS:
        return action_str
    lower = action_str.lower()
    for key in ACTION_LABELS:
        if key.lower() in lower:
            return key
    return None


# ─────────── Cog ───────────

class MoonlightAuditLog(commands.Cog):
    """Poll Roblox group audit logs and post new entries to a Discord channel."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=92710046, force_registration=True)
        self.config.register_guild(
            is_main_guild=False,
            audit_channel_id=None,
            poll_interval=30,   # seconds between polls
        )
        # last_seen is kept in-memory only; baseline is re-established on reload.
        self._last_seen = {}   # group_id -> most-recent-entry-timestamp string
        self._poll_task = None

    async def cog_load(self):
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    async def cog_unload(self):
        if self._poll_task:
            self._poll_task.cancel()

    # ─────────── Main guild lookup ───────────

    async def _get_main_guild(self):
        for guild in self.bot.guilds:
            if await self.config.guild(guild).is_main_guild():
                return guild
        return None

    async def _get_channel(self):
        guild = await self._get_main_guild()
        if guild is None:
            return None
        channel_id = await self.config.guild(guild).audit_channel_id()
        if not channel_id:
            return None
        ch = guild.get_channel(channel_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    # ─────────── Polling ───────────

    async def _set_baseline(self, api_key):
        """Record the most recent entry per group so we only post NEW things from here on."""
        for group_id in MONITORED_GROUPS:
            entries = await fetch_audit_log(group_id, api_key)
            if entries:
                self._last_seen[group_id] = entries[0].get("created", "")
            else:
                self._last_seen[group_id] = ""
            # Small delay between groups to avoid burst rate-limits
            await asyncio.sleep(0.5)

    async def _poll_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)  # small boot grace period

        api_key = os.environ.get("ROBLOX_API_KEY", "")
        if not api_key:
            print("[auditlog] ROBLOX_API_KEY not set — audit log polling disabled.")
            return

        await self._set_baseline(api_key)
        print(f"[auditlog] Baseline set for {len(MONITORED_GROUPS)} groups.")

        while not self.bot.is_closed():
            try:
                guild = await self._get_main_guild()
                if guild is None:
                    await asyncio.sleep(60)
                    continue

                interval = await self.config.guild(guild).poll_interval()
                await asyncio.sleep(max(15, int(interval)))

                channel = await self._get_channel()
                if channel is None:
                    continue

                for group_id, group_name in MONITORED_GROUPS.items():
                    await self._check_group(group_id, group_name, channel, api_key)
                    # Space out per-group requests
                    await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[auditlog] poll loop error: {e}")
                await asyncio.sleep(60)

    async def _check_group(self, group_id, group_name, channel, api_key):
        entries = await fetch_audit_log(group_id, api_key)
        if not entries:
            return

        last_ts = self._last_seen.get(group_id, "")

        # Collect entries newer than last_ts. Uses timestamp ordering so reordering/pagination
        # in Roblox's response won't cause us to miss or duplicate events.
        last_unix = parse_ts(last_ts) if last_ts else 0
        new_entries = []
        newest_ts = last_ts
        newest_unix = last_unix

        for e in entries:
            ts = e.get("created", "")
            u = parse_ts(ts) or 0
            if u > last_unix:
                new_entries.append(e)
                if u > newest_unix:
                    newest_unix = u
                    newest_ts = ts

        if not new_entries:
            return

        # Advance cursor to the newest we've seen
        self._last_seen[group_id] = newest_ts

        # Post oldest-first so the channel reads in chronological order
        for entry in reversed(new_entries):
            action_raw = str(entry.get("actionType", ""))
            action_key = match_action(action_raw)
            if not action_key:
                print(f"[auditlog] {group_name}: skipping unmapped action '{action_raw}'")
                continue
            try:
                await self._send_entry(channel, group_name, entry, action_key)
            except discord.HTTPException as e:
                print(f"[auditlog] send error: {e}")

    async def _send_entry(self, channel, group_name, entry, action_key):
        actor = entry.get("actor", {}) or {}
        actor_user = actor.get("user", {}) or {}
        actor_id = str(actor_user.get("userId", "?"))
        actor_name = actor_user.get("username", "Unknown")
        actor_role = (actor.get("role") or {}).get("name", "")

        desc = entry.get("description", {}) or {}
        target_id = str(desc.get("TargetId", "") or "")
        target_name = desc.get("TargetName", "") or desc.get("TargetDisplayName", "")
        if not target_name and target_id.isdigit():
            target_name = await get_roblox_username(target_id)

        role_name = desc.get("RoleSetName", "") or desc.get("NewRoleSetName", "")
        old_role = desc.get("OldRoleSetName", "")

        created_ts = parse_ts(entry.get("created", ""))
        color = ACTION_COLORS.get(action_key, 0x2b2d31)
        label = ACTION_LABELS.get(action_key, action_key)

        embed = discord.Embed(
            title=label,
            description=f"**Group:** {group_name}",
            color=color,
        )

        actor_field = f"**{actor_name}** (`{actor_id}`)"
        if actor_role:
            actor_field += f"\n**Role:** {actor_role}"
        if actor_id.isdigit():
            actor_field += f"\n[Profile](https://www.roblox.com/users/{actor_id}/profile)"
        embed.add_field(name="Actor", value=actor_field, inline=True)

        if target_name and target_id:
            target_field = f"**{target_name}** (`{target_id}`)"
            if target_id.isdigit():
                target_field += f"\n[Profile](https://www.roblox.com/users/{target_id}/profile)"
            embed.add_field(name="Target", value=target_field, inline=True)

        if old_role and role_name:
            embed.add_field(name="Change", value=f"{old_role} → **{role_name}**", inline=False)
        elif role_name:
            embed.add_field(name="Role", value=f"**{role_name}**", inline=False)

        if created_ts:
            embed.add_field(name="Date", value=f"<t:{created_ts}:F>", inline=False)

        embed.set_footer(text=footer_text())
        await channel.send(embed=embed)

    # ─────────── Commands ───────────

    @commands.group(name="auditlogset")
    @commands.admin_or_permissions(manage_guild=True)
    async def auditlogset(self, ctx: commands.Context):
        """Configure MoonlightAuditLog."""

    @auditlogset.command(name="main")
    async def set_main(self, ctx: commands.Context, on_off: bool = True):
        """Mark this server as the main guild (where audit logs get posted)."""
        if on_off:
            for g in self.bot.guilds:
                if g.id != ctx.guild.id:
                    await self.config.guild(g).is_main_guild.set(False)
        await self.config.guild(ctx.guild).is_main_guild.set(on_off)
        await ctx.send(f"Main guild flag: **{'on' if on_off else 'off'}**.")

    @auditlogset.command(name="channel")
    async def set_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the audit log channel."""
        if channel is None:
            await self.config.guild(ctx.guild).audit_channel_id.set(None)
            return await ctx.send("Audit log channel cleared.")
        await self.config.guild(ctx.guild).audit_channel_id.set(channel.id)
        await ctx.send(f"Audit log channel set to {channel.mention}.")

    @auditlogset.command(name="poll")
    async def set_poll(self, ctx: commands.Context, seconds: int):
        """Set poll interval in seconds (minimum 15)."""
        seconds = max(15, seconds)
        await self.config.guild(ctx.guild).poll_interval.set(seconds)
        await ctx.send(f"Poll interval set to **{seconds}s** (~{int(3600/seconds * len(MONITORED_GROUPS))} API calls/hour).")

    @auditlogset.command(name="resetbaseline")
    async def reset_baseline(self, ctx: commands.Context):
        """Reset the 'last seen' cursor so the next poll re-baselines instead of flooding."""
        api_key = os.environ.get("ROBLOX_API_KEY", "")
        if not api_key:
            return await ctx.send("❌ ROBLOX_API_KEY is not set.")
        await self._set_baseline(api_key)
        await ctx.send(f"✅ Baseline reset for {len(MONITORED_GROUPS)} groups.")

    @auditlogset.command(name="test")
    async def test_one(self, ctx: commands.Context, group_id: int):
        """Fetch the most recent audit entry for a group and post it (debug only)."""
        api_key = os.environ.get("ROBLOX_API_KEY", "")
        if not api_key:
            return await ctx.send("❌ ROBLOX_API_KEY is not set.")

        entries = await fetch_audit_log(group_id, api_key)
        if not entries:
            return await ctx.send(f"No data returned for group `{group_id}` (check key/permissions).")
        entry = entries[0]
        action_raw = str(entry.get("actionType", ""))
        action_key = match_action(action_raw)
        if not action_key:
            return await ctx.send(f"Got entry but action `{action_raw}` is not in the mapping table.")
        group_name = MONITORED_GROUPS.get(group_id, f"Group {group_id}")
        await self._send_entry(ctx.channel, group_name, entry, action_key)

    @auditlogset.command(name="show")
    async def show(self, ctx: commands.Context):
        """Show current settings and status."""
        data = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(data["audit_channel_id"]) if data["audit_channel_id"] else None

        api_status = "✅ Set" if os.environ.get("ROBLOX_API_KEY") else "❌ Not set (env var ROBLOX_API_KEY)"
        cached = sum(1 for ts in self._last_seen.values() if ts)

        embed = discord.Embed(title="MoonlightAuditLog Settings", color=0x5865f2)
        embed.add_field(name="Main guild", value="Yes" if data.get("is_main_guild") else "No", inline=False)
        embed.add_field(name="Audit channel", value=ch.mention if ch else "Not set", inline=False)
        embed.add_field(name="Poll interval", value=f"{data['poll_interval']}s", inline=False)
        embed.add_field(name="ROBLOX_API_KEY", value=api_status, inline=False)
        embed.add_field(name="Monitored groups", value=f"{len(MONITORED_GROUPS)} (hardcoded)", inline=False)
        embed.add_field(name="Baseline cursors in memory", value=f"{cached}/{len(MONITORED_GROUPS)}", inline=False)
        await ctx.send(embed=embed)
