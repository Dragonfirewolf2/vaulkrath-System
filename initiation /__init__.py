from .initiation import RitualInitiation


async def setup(bot):
    await bot.add_cog(MoonlightRitual(bot))
