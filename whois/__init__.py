from .whois import Whois


async def setup(bot):
    await bot.add_cog(Whois(bot))
