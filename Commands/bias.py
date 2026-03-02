import discord
from discord import app_commands
from discord.ext import commands

from Core.pws_bias import run_bias_snapshot

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


def fmt_station_bias_block(st: dict, reference_times: dict) -> str:
    def fnum(x):
        return "N/A" if x is None else f"{x:.2f}"

    station_id = st.get("station_id", "UNKNOWN")
    station_label = STATION_LABELS.get(station_id, "Station ?")

    status = st.get("status")
    status_str = str(status) if status is not None else ("OK" if st.get("ok") else "ERR")

    lines = [
        f"{station_label}",
        f"Status: {status_str}",
        "Bias Per Hour (NWS, PWS):",
    ]

    bias_by_time = st.get("bias_by_time") or {}

    def time_key(hhmm):
        h, m = hhmm.split(":")
        return int(h), int(m)

    for hhmm in sorted(bias_by_time.keys(), key=time_key):
        bias = bias_by_time[hhmm]
        nws = reference_times.get(hhmm)
        pws = None if nws is None else nws + bias
        lines.append(f"{hhmm}: {fnum(nws)}, {fnum(pws)} Δ={fnum(bias)}")

    if not bias_by_time:
        lines.append("N/A")

    lines += [
        f"Median Bias: {fnum(st.get('median_bias'))}",
        f"Weighted Bias: {fnum(st.get('weighted_bias'))}",
        f"Curr PWS Temp: {fnum(st.get('pws_now'))}",
        f"Predicted NWS Median Bias: {fnum(st.get('pred_nws_median'))}",
        f"Predicted NWS Weighted Bias: {fnum(st.get('pred_nws_weighted'))}",
        f"Agree Diff: {fnum(st.get('agree_diff'))}",
    ]

    if not st.get("ok") and st.get("error"):
        lines.append(f"Error: {st['error']}")

    return "\n".join(lines)


class Bias(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="bias", description="Show PWS bias vs NWS by station.")
    async def bias(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        data = run_bias_snapshot()
        ref = data.get("reference_times", {})
        stations = data.get("stations", [])

        blocks = [fmt_station_bias_block(st, ref) for st in stations]

        LIMIT = 1900
        current = ""

        for b in blocks:
            add = ("" if current == "" else "\n\n") + b
            if len(current) + len(add) > LIMIT:
                await interaction.followup.send(f"```text\n{current}\n```")
                current = b
            else:
                current += add

        if current:
            await interaction.followup.send(f"```text\n{current}\n```")


async def setup(bot: commands.Bot):
    await bot.add_cog(Bias(bot))
