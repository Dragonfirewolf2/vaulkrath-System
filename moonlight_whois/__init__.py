from .moonlight_whois import MoonlightWhois


async def setup(bot):
    await bot.add_cog(MoonlightWhois(bot))
