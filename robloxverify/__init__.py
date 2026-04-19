from .robloxverify import RobloxVerify


async def setup(bot):
    await bot.add_cog(RobloxVerify(bot))
