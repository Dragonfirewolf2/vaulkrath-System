from .leadership import Leadership


async def setup(bot):
    await bot.add_cog(Leadership(bot))
