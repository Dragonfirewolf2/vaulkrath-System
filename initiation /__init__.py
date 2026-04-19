from .initiation import Initiation


async def setup(bot):
    await bot.add_cog(Initiation(bot))
