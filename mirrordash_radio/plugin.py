import asyncio
import hmac
import json
import logging
import os
import re
import urllib.parse

from fastapi import (  # on the mirror already: not in pyproject's dependencies
    APIRouter,
    Header,
    HTTPException,
)

logger = logging.getLogger("mirrordash.modules.mirrordash_radio")

# Radio Browser (https://www.radio-browser.info): a free, open list of some 50 000 stations, no key.
# all.api is every server of theirs behind one name.
SEARCH_URL = "https://all.api.radio-browser.info/json/stations/search"
USER_AGENT = "MirrorDash-Radio/1.0 (+https://github.com/Menturan/mirrordash-radio)"  # they ask for a telling one
DAY = 86400  # a station's address and logo change rarely: looked up again once a day at most
# The mirror's sound outputs, by name: their numbers change between boots
OUTPUTS = {"jack": "plughw:CARD=Headphones,DEV=0", "hdmi": "hdmi:CARD=vc4hdmi,DEV=0"}
TITLE = re.compile(r"^\s*title: (.+)$")  # gst-launch -t prints a stream's tags; SR sends its programme as the title
RETRY = (5, 10, 30, 60)  # seconds before a stream that ended is started again, then every minute
# What a URL needs and gst-launch's pipeline parser leaves alone. Everything else is percent-encoded: with a \"
# in it, an address could end the uri and add elements of its own (a station in Radio Browser is anyone's)
URL_SAFE = ":/?#[]@&=+$,;%~"

# Shown in the admin page's preview, which never plays: a station like Radio Browser's
EXAMPLE = {"name": "Nordglimt FM", "url": "https://example.com/stream.mp3", "logo": "", "title": "Morning Coffee"}  # made up


def parse_station(entry: str, answer) -> dict | None:
    """A station from what the owner typed: an http(s) address as it is, or else Radio Browser's most played
    match for the name, its best sound among the entries of that name (the most played P1 is the 32 kbit/s one).
    None when neither gives an address."""
    entry = entry.strip()
    if is_stream_url(entry):
        return {"name": urllib.parse.urlsplit(entry).hostname or entry, "url": entry, "logo": ""}
    if not answer or not isinstance(answer, list) or not all(isinstance(s, dict) for s in answer):
        return None
    name = (answer[0].get("name") or "").strip()
    same = [s for s in answer if (s.get("name") or "").strip() == name and is_stream_url(s.get("url_resolved") or s.get("url") or "")]
    best = max(same, key=lambda s: s.get("bitrate") or 0) if same else answer[0]
    url = best.get("url_resolved") or best.get("url") or ""
    if not is_stream_url(url):
        return None
    logo = best.get("favicon") or ""
    return {"name": (best.get("name") or entry).strip(), "url": url, "logo": logo if logo.startswith("https://") else ""}


def is_stream_url(text: str) -> bool:
    """Only http(s): GStreamer would play file:// and others too, and a command from Home Assistant or the
    module's web address must play an address, nothing else."""
    return urllib.parse.urlsplit(text).scheme in ("http", "https") and len(text) <= 2000


def cubic(volume: int) -> float:
    """The owner's 0-100 as GStreamer's linear volume, on the hearing scale (as the Settings tab's volume):
    on the linear one 50 % sounds nearly as loud as 100 %."""
    return round((max(0, min(100, volume)) / 100) ** 3, 4)


def player_command(url: str, volume: int, output: str) -> list[str]:
    """gst-launch for one stream. setpriv --pdeathsig: the player gets SIGTERM when the module's process ends,
    also by a crash or a kill, so a stopped module never leaves the radio playing (preexec_fn could do the same,
    but isn't safe in a process with threads, which the module's has)."""
    return ["setpriv", "--pdeathsig", "TERM", "--", "gst-launch-1.0", "-t", "playbin3",
            f"uri={urllib.parse.quote(url, safe=URL_SAFE)}", f"volume={cubic(volume)}", f"audio-sink=alsasink device={output}"]


class RadioModule:
    def __init__(self, config):
        self.config = config
        self.name = "mirrordash_radio"
        self.interval = 60  # the card again once a minute at most (a new title comes at once anyway)
        self.changed = asyncio.Event()
        self.preview = config.get("preview", False)  # the admin page's preview: never plays
        self.entries = [str(s.get("station", "")).strip() for s in config.get("stations", [])
                        if isinstance(s, dict) and str(s.get("station", "")).strip()]
        self.volume = int(config.get("volume", 35))
        self.output = OUTPUTS.get(config.get("output"), OUTPUTS["jack"])
        self.autoplay = config.get("autoplay", False)
        self.show_logo = config.get("show_logo", True)
        self.show_header = config.get("show_header", False)
        self.button = config.get("button", "")  # which of the mirror's buttons controls it; "" none
        self.token = config.get("token", "")

        # Kept in data_dir, so a saved setting (which restarts the module) doesn't stop the radio or lose the station
        self.state_file = os.path.join(config["data_dir"], "state.json") if config.get("data_dir") else None
        state = self.load_state()
        self.wanted = state.get("playing", self.autoplay) is True  # the owner wants it playing (it may be hidden)
        self.index = state.get("index") if isinstance(state.get("index"), int) else 0
        self.url_override = None  # an address sent from Home Assistant or the web address, until the next station
        self.shown = False  # nothing plays until the mirror says the card is on the screen (on_shown)
        self.stations: dict[str, dict | None] = {}  # what each entry resolved to
        self.player = None
        self.player_task = None
        self.title = ""
        self.problem = None  # (translation key, English) for the card

        self.router = APIRouter()
        self.router.post("/play")(self.on_play)
        self.router.post("/stop")(self.on_stop)
        event_bus = config.get("event_bus")  # not there in a test
        if event_bus and self.button:
            event_bus.subscribe("hardware.button", self.on_button)

    # ── The card ──
    async def run_loop(self, broadcast_func):
        try:
            while True:
                await broadcast_func(self.name, await self.render())
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), self.interval)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            await self.stop_player()
            raise

    async def render(self) -> str:
        context = {"show_logo": self.show_logo, "show_header": self.show_header}
        if self.preview:
            return self.render_template("widget.html", station=EXAMPLE, title=EXAMPLE["title"], playing=True,
                                        message=None, **context)
        station = await self.current_station()
        message = None
        if station is None and not self.entries:
            message = self.translate("no_stations", "Add a station in the module's settings.")
        elif station is None:
            message = self.translate("not_found", "Can't find the station. Check its name, or give its address.")
        elif self.problem:
            message = self.translate(*self.problem)
        return self.render_template("widget.html", station=station, title=self.title, playing=self.playing(),
                                    message=message, **context)

    def playing(self) -> bool:
        return self.player is not None and self.player.returncode is None

    async def current_station(self) -> dict | None:
        if self.url_override:
            return parse_station(self.url_override, None)
        if not self.entries:
            return None
        entry = self.entries[self.index % len(self.entries)]
        if self.stations.get(entry) is None:
            answer = None
            if not is_stream_url(entry):
                answer, _ = await self.fetch_json(SEARCH_URL, headers={"User-Agent": USER_AGENT}, max_age=DAY, params={
                    "name": entry, "order": "clickcount", "reverse": "true", "hidebroken": "true", "limit": "10"})
            self.stations[entry] = parse_station(entry, answer)
        return self.stations[entry]

    # ── Playing ──
    async def on_shown(self, shown: bool):
        """The mirror: the card is on the screen or not (the screen off, the module's schedule, privacy mode).
        Hidden, the sound stops; shown again, it plays if the owner wanted it."""
        self.shown = shown
        await self.apply()

    async def apply(self):
        """Play while wanted and shown, else stop; the card follows."""
        if self.wanted and self.shown and not self.preview:
            if not (self.player_task and not self.player_task.done()):
                self.player_task = asyncio.create_task(self.keep_playing())
        else:
            await self.stop_player()
        self.changed.set()

    async def keep_playing(self):
        """The player while it should play: started again after a stream ends or fails, a little later each time."""
        tries = 0
        while self.wanted and self.shown:
            station = await self.current_station()
            if station is None:
                self.changed.set()
                return
            self.title, self.problem = "", None
            try:
                self.player = await asyncio.create_subprocess_exec(
                    *player_command(station["url"], self.volume, self.output),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            except OSError as e:  # no setpriv or GStreamer: a computer that isn't a mirror
                logger.warning(f"Can't start the player: {e}")
                self.problem = ("no_player", "Can't play sound here.")
                self.changed.set()
                return
            self.changed.set()
            await self.read_titles(self.player.stdout)
            await self.player.wait()
            if not (self.wanted and self.shown):
                return
            # the host only: an address can hold a key (?token=…), and the log is the owner's to share
            logger.info(f"The stream from {urllib.parse.urlsplit(station['url']).hostname} ended ({self.player.returncode})")
            self.problem = ("stream_failed", "The station doesn't answer. Trying again.")
            self.changed.set()
            await asyncio.sleep(RETRY[min(tries, len(RETRY) - 1)])
            tries += 1

    async def read_titles(self, stdout):
        """The stream's titles from gst-launch -t's output. Its progress comes as \\r-separated text without a
        newline for a long time, so it is read in pieces and split on both."""
        pending = b""
        while chunk := await stdout.read(4096):
            *lines, pending = re.split(rb"[\r\n]", pending + chunk)
            pending = pending[-4096:]
            for line in lines:
                if match := TITLE.match(line.decode(errors="replace")):
                    title = match.group(1).strip()[:200]
                    if title != self.title:
                        self.title = title
                        await self.share("now_playing", title)
                        self.changed.set()

    async def stop_player(self):
        task, self.player_task = self.player_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if self.player and self.player.returncode is None:
            self.player.terminate()
            try:
                await asyncio.wait_for(self.player.wait(), 3)
            except TimeoutError:
                self.player.kill()
        self.player, self.title = None, ""

    async def set_playing(self, on: bool):
        self.wanted = on
        self.save_state()
        await self.share("playing", on)
        await self.apply()

    async def next_station(self):
        if not self.entries:
            return
        self.url_override = None
        self.index = (self.index + 1) % len(self.entries)
        self.save_state()
        await self.stop_player()
        station = await self.current_station()
        await self.share("station", station["name"] if station else "")
        await self.apply()

    async def play_url(self, url: str):
        self.url_override = url
        await self.stop_player()
        await self.share("station", parse_station(url, None)["name"])
        await self.set_playing(True)

    # ── The mirror's button ──
    async def on_button(self, data: dict):
        """One press plays or stops, two go to the next station: on the button the owner chose for the radio."""
        if data.get("button") != self.button:
            return
        if data.get("press") == "single":
            await self.set_playing(not self.wanted)
        elif data.get("press") == "double":
            await self.next_station()

    # ── Its own web address ── for a phone shortcut, with the Token setting
    def check_token(self, x_token: str):
        """Anyone on the home network can call it: without the right token it does nothing (none set: never)."""
        if not self.token or not hmac.compare_digest(x_token.encode(), self.token.encode()):
            raise HTTPException(status_code=403)

    async def on_play(self, body: dict | None = None, x_token: str = Header(default="")):
        """POST <api_path>/play: plays, or with {"url": "https://…"} plays that stream."""
        self.check_token(x_token)
        url = str((body or {}).get("url") or "")
        if url and not is_stream_url(url):
            raise HTTPException(status_code=400, detail="url must be an http(s) address")
        await (self.play_url(url) if url else self.set_playing(True))
        return {"ok": True}

    async def on_stop(self, x_token: str = Header(default="")):
        """POST <api_path>/stop."""
        self.check_token(x_token)
        await self.set_playing(False)
        return {"ok": True}

    # ── Home Assistant (MQTT) ──
    async def share(self, name: str, value):
        """A value of something "exposes" declares. A mirror before MirrorDash 0.10 has no self.expose."""
        if hasattr(self, "expose"):
            await self.expose(name, value)

    async def on_control(self, name: str, value):
        """Home Assistant's commands, already checked against config_schema.json's "exposes"."""
        if name == "playing":
            await self.set_playing(bool(value))
        elif name == "next_station":
            await self.next_station()
        elif name == "volume":
            self.volume = int(value)  # ponytail: until the module restarts; the setting is the lasting volume
            await self.share("volume", self.volume)
            if self.playing():  # ponytail: gst-launch can't change its volume, so the stream starts again (a short gap)
                await self.stop_player()
                await self.apply()
        elif name == "play_url" and is_stream_url(str(value)):
            await self.play_url(str(value))

    # ── Saving ──
    def load_state(self) -> dict:
        if not self.state_file:
            return {}
        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self) -> None:
        if self.state_file:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"playing": self.wanted, "index": self.index}, f)
