import asyncio
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from mirrordash_radio import plugin
from mirrordash_radio.plugin import RadioModule, cubic, is_stream_url, parse_station

# Radio Browser's answer, most played first: the best sound of that name wins, another name never does
P1 = [{"name": "Sveriges Radio - P1 ", "url_resolved": "https://live1.sr.se/p1-aac-32", "bitrate": 32, "favicon": ""},
      {"name": "Sveriges Radio - P1", "url": "http://x/p1", "url_resolved": "https://live1.sr.se/p1-aac-320",
       "bitrate": 320, "favicon": "https://static-cdn.sr.se/p1.png"},
      {"name": "P1 Denmark", "url_resolved": "https://dr.dk/p1-hq", "bitrate": 999}]


def make_module(tmp_path=None, **settings):
    """The module as the mirror sets it up, with the mirror's helpers replaced by fakes."""
    config = {"stations": [{"station": "Sveriges Radio P1"}, {"station": "https://example.com/jazz.mp3"}], **settings}
    if tmp_path:
        config["data_dir"] = str(tmp_path)
    module = RadioModule(config)
    module.render_template = MagicMock(side_effect=lambda name, **context: context)
    module.translate = lambda key, default=None: default
    module.fetch_json = AsyncMock(return_value=(P1, None))
    module.expose = AsyncMock()
    return module


class FakePlayer:
    """gst-launch-1.0 -t: prints its tags, then plays until it is stopped."""
    def __init__(self, output=b""):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        self.stopped = asyncio.Event()

    def terminate(self):
        self.returncode = -15
        self.stdout.feed_eof()
        self.stopped.set()

    kill = terminate

    async def wait(self):
        await self.stopped.wait()
        return self.returncode


def test_a_station_is_an_address_or_radio_browsers_best_match():
    assert parse_station("Sveriges Radio P1", P1) == {"name": "Sveriges Radio - P1", "url": "https://live1.sr.se/p1-aac-320",
                                                      "logo": "https://static-cdn.sr.se/p1.png"}
    assert parse_station("https://example.com/jazz.mp3", None)["url"] == "https://example.com/jazz.mp3"
    assert parse_station("Nowhere FM", []) is None
    assert parse_station("Bad", [{"url_resolved": "file:///etc/passwd"}]) is None  # only http(s) plays
    assert parse_station("Logo", [{"url_resolved": "https://a/s", "favicon": "http://a/l.png"}])["logo"] == ""


INJECTION = 'https://a.invalid/x\\" ! fakesink name=injected'  # a \" ended the uri for gst-launch's parser


def test_an_address_can_only_be_an_address_to_gst_launch():
    """Percent-encoded, nothing in an address is read by gst-launch's parser: no \\ " ' ! or space reaches it."""
    uri = plugin.player_command(INJECTION, 35, "null")[7]
    assert uri == "uri=https://a.invalid/x%5C%22%20%21%20fakesink%20name=injected"
    assert plugin.player_command("https://a/s.mp3?k=1&b=%20", 35, "null")[7] == "uri=https://a/s.mp3?k=1&b=%20"


@pytest.mark.skipif(not shutil.which("gst-launch-1.0"), reason="no GStreamer here")
def test_gst_launch_itself_makes_no_element_of_an_address():
    """The real parser: before the fix it made the element and said "could not link playbin3-0 to injected"."""
    command = plugin.player_command(INJECTION, 0, "null")[4:]
    out = subprocess.run(["timeout", "4", *command], capture_output=True, text=True, check=False)
    assert "could not link" not in out.stdout + out.stderr


def test_only_web_addresses_play_and_the_volume_follows_the_ear():
    assert is_stream_url("https://a/s.mp3") and not is_stream_url("file:///etc/passwd") and not is_stream_url("rtsp://a")
    assert cubic(100) == 1 and cubic(50) == 0.125 and cubic(0) == 0 and cubic(250) == 1


@pytest.mark.asyncio
async def test_the_card_shows_the_station_and_looks_it_up_once():
    module = make_module()
    shown = await module.render()
    assert shown["station"]["name"] == "Sveriges Radio - P1" and shown["playing"] is False and shown["message"] is None
    await module.render()
    module.fetch_json.assert_awaited_once()
    assert module.fetch_json.call_args.kwargs["params"]["name"] == "Sveriges Radio P1"
    assert module.fetch_json.call_args.kwargs["headers"]["User-Agent"].startswith("MirrorDash-Radio")


@pytest.mark.asyncio
async def test_no_stations_or_one_not_found_says_so():
    assert "Add a station" in (await make_module(stations=[]).render())["message"]
    module = make_module()
    module.fetch_json.return_value = ([], None)
    assert "Can't find" in (await module.render())["message"]


@pytest.mark.asyncio
async def test_it_plays_only_while_wanted_and_shown_and_shows_the_streams_title(tmp_path):
    module = make_module(tmp_path, autoplay=True, volume=50, output="hdmi")
    player = FakePlayer(b"buffering... 5%\rbuffering... 100%\r\n           title: Studio Ett\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)) as start:
        await module.render()
        assert not start.await_count  # not before the mirror says the card is shown
        await module.on_shown(True)
        await asyncio.sleep(0.01)
        args = start.call_args.args
        assert args[:7] == ("setpriv", "--pdeathsig", "TERM", "--", "gst-launch-1.0", "-t", "playbin3")
        assert "uri=https://live1.sr.se/p1-aac-320" in args
        assert "volume=0.125" in args and "audio-sink=alsasink device=hdmi:CARD=vc4hdmi,DEV=0" in args
        shown = await module.render()
        assert shown["playing"] and shown["title"] == "Studio Ett"
        module.expose.assert_any_await("now_playing", "Studio Ett")

        await module.on_shown(False)  # the screen off, its schedule, privacy mode
        assert player.returncode == -15 and not (await module.render())["playing"]
        assert module.wanted  # still wanted: back on when shown again
        await module.on_shown(True)
        await asyncio.sleep(0.01)
        assert start.await_count == 2
        await module.stop_player()


@pytest.mark.asyncio
async def test_the_button_plays_stops_and_steps_and_a_restart_remembers(tmp_path):
    module = make_module(tmp_path, button="button_2")
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: FakePlayer())) as start:
        await module.on_shown(True)
        await module.on_button({"button": "button", "press": "single"})  # another button: not the radio's
        assert not module.wanted
        await module.on_button({"button": "button_2", "press": "single"})
        await asyncio.sleep(0.01)
        assert module.playing() and start.await_count == 1
        await module.on_button({"button": "button_2", "press": "double"})
        await asyncio.sleep(0.01)
        assert "uri=https://example.com/jazz.mp3" in start.call_args.args
        module.expose.assert_any_await("station", "example.com")
        await module.stop_player()

    restarted = make_module(tmp_path, button="button_2")  # a saved setting restarts it
    assert restarted.wanted and restarted.index == 1


@pytest.mark.asyncio
async def test_home_assistant_and_the_web_address_control_it():
    module = make_module(token="s3cret")
    module.set_playing = AsyncMock()
    module.play_url = AsyncMock()
    module.next_station = AsyncMock()
    await module.on_control("playing", True)
    module.set_playing.assert_awaited_with(True)
    await module.on_control("next_station", None)
    module.next_station.assert_awaited_once()
    await module.on_control("play_url", "file:///etc/passwd")
    module.play_url.assert_not_awaited()
    await module.on_control("play_url", "https://a/s.mp3")
    module.play_url.assert_awaited_with("https://a/s.mp3")

    for token in ("", "wrong"):
        with pytest.raises(HTTPException):
            await module.on_play({}, x_token=token)
    with pytest.raises(HTTPException):
        await module.on_play({"url": "file:///x"}, x_token="s3cret")
    assert await module.on_play({}, x_token="s3cret") == {"ok": True}
    assert await module.on_stop(x_token="s3cret") == {"ok": True}
    module.set_playing.assert_awaited_with(False)
    with pytest.raises(HTTPException):
        await make_module().on_stop(x_token="")  # no token set: nobody


@pytest.mark.asyncio
async def test_the_preview_shows_an_example_and_never_plays():
    module = make_module(preview=True, autoplay=True)
    with patch("asyncio.create_subprocess_exec", AsyncMock()) as start:
        await module.on_shown(True)
        shown = await module.render()
    assert shown["station"]["name"] == "Nordglimt FM" and shown["playing"]
    start.assert_not_awaited()
    module.fetch_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_computer_without_gstreamer_says_so():
    module = make_module(autoplay=True)
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("gst-launch-1.0"))):
        await module.on_shown(True)
        await asyncio.sleep(0.01)
    assert (await module.render())["message"] == "Can't play sound here."
