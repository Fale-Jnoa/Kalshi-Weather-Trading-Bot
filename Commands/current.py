import discord
from discord import app_commands
from discord.ext import commands

from Core.pws_current import current_pws_data


# Private mapping: real station IDs -> public labels
STATION_LABELS = {
    "KNYNEWYO1796": "Station 1",
    "KNYNEWYO1115": "Station 2",
    "KNYNEWYO1967": "Station 3",
    "KNYNEWYO270":  "Station 4",
    "KNYNEWYO1024": "Station 5",
    "KNYNEWYO1238": "Station 6",
    "KNYNEWYO2109": "Station 7",
}


class Current(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="current", description="Show current temps for all stations.")
    async def current(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        data = current_pws_data()
        stations = data.get("stations", {})

        lines = []
        for sid, info in stations.items():
            label = STATION_LABELS.get(sid, "Station ?")

            if info.get("ok"):
                temp = info.get("temp_f")
                tstr = f"{temp:.2f}" if temp is not None else "N/A"
                lines.append(f"{label}: {tstr}°F")
            else:
                status = info.get("status")
                lines.append(f"{label}: N/A (status={status})")

        msg = "\n".join(lines) if lines else "No station data."

        if len(msg) > 1900:
            msg = msg[:1900] + "\n... (truncated)"

        await interaction.followup.send(f"```text\n{msg}\n```")


async def setup(bot: commands.Bot):
    await bot.add_cog(Current(bot))
