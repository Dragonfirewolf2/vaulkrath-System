from .moonlight_auditlog import MoonlightAuditLog


async def setup(bot):
    await bot.add_cog(MoonlightAuditLog(bot))
