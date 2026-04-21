from .moonlight_leadership import MoonlightLeadership


async def setup(bot):
    await bot.add_cog(MoonlightLeadership(bot))
