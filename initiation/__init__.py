from .moonlight_ritual import MoonlightRitual


async def setup(bot):
    await bot.add_cog(MoonlightRitual(bot))
