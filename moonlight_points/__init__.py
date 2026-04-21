from .moonlight_points import MoonlightPoints


async def setup(bot):
    await bot.add_cog(MoonlightPoints(bot))
