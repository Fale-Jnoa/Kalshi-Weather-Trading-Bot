import discord
from discord import app_commands
from discord.ext import commands

from Core.hourback_service import hourly_report_all


# Public station numbers → private station IDs
STATION_ID_FROM_NUM = {
    1: "KNYNEWYO1796",
    2: "KNYNEWYO1115",
    3: "KNYNEWYO1967",
    4: "KNYNEWYO270",
    5: "KNYNEWYO1024",
    6: "KNYNEWYO1238",
    7: "KNYNEWYO2109",
}

# Reverse map for display
STATION_LABELS = {v: f"Station {k}" for k, v in STATION_ID_FROM_NUM.items()}


def _fmt_dt(dt):
    return "N/A" if dt is None else dt.strftime("%Y-%m-%d %H:%M:%S")


def _fnum(x):
    return "N/A" if x is None else f"{x:.2f}"


def fmt_station_line(st: dict) -> str:
    sid = st.get("station_id")
    label = STATION_LABELS.get(sid, "Station ?")
    status = st.get("status")

    if not st.get("ok"):
        return f"{label}: error (status={status})"

    now = st["now"]
    one = st["one_hour_ago"]
    high = st["high_1h"]

    now_line = f"Now={_fnum(now.get('temp_f'))}F @ {_fmt_dt(now.get('dt_local'))}"

    if one.get("ok"):
        one_line = (
            f"1hAgo={_fnum(one.get('temp_f'))}F @ {_fmt_dt(one.get('dt_local'))} "
            f"(off by {_fnum(one.get('off_by_minutes'))}m)"
        )
    else:
        off = one.get("off_by_minutes")
        one_line = (
            f"1hAgo: no close match (closest was {_fnum(off)} min away)"
            if off is not None else "1hAgo: no match"
        )

    d = st.get("delta_1h_f")
    delta_line = "Δ(1h)=N/A" if d is None else f"Δ(1h)={d:+.2f}F"

    if high.get("temp_f") is None:
        high_line = "1hHigh: no data in window"
    else:
        high_line = f"1hHigh={_fnum(high.get('temp_f'))}F @ {_fmt_dt(high.get('dt_local'))}"

    return f"{label} | {now_line} | {one_line} | {delta_line} | {high_line}"


class HourBack(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="hourback",
        description="Shows current temp, ~1h-ago temp, 1h delta, and 1h high."
    )
    @app_commands.describe(
        station="Optional station number (1–7)",
        match_tol_min="Tolerance (minutes) for matching the ~1 hour-ago observation"
    )
    async def hourback(
        self,
        interaction: discord.Interaction,
        station: int | None = None,
        match_tol_min: int = 10,
    ):
        await interaction.response.defer(thinking=True)

        # Map station number → internal ID
        if station is not None:
            station_id = STATION_ID_FROM_NUM.get(station)
            if not station_id:
                await interaction.followup.send("Invalid station number. Use 1–7.")
                return
            station_ids = [station_id]
        else:
            station_ids = None

        data = hourly_report_all(
            station_ids=station_ids,
            match_tol_min=match_tol_min,
        )

        lines = [fmt_station_line(s) for s in data.get("stations", [])]

        if not lines:
            await interaction.followup.send("No station data.")
            return

        LIMIT = 1900
        chunk = ""
        for line in lines:
            add = ("" if chunk == "" else "\n") + line
            if len(chunk) + len(add) > LIMIT:
                await interaction.followup.send(f"```text\n{chunk}\n```")
                chunk = line
            else:
                chunk += add

        if chunk:
            await interaction.followup.send(f"```text\n{chunk}\n```")


async def setup(bot: commands.Bot):
    await bot.add_cog(HourBack(bot))
