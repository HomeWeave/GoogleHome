import os
from threading import Thread, Event
from uuid import UUID

import zeroconf
import pychromecast
from pychromecast.const import CAST_TYPE_CHROMECAST, CAST_TYPE_AUDIO
from pychromecast.const import CAST_TYPE_GROUP

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import AppHandlerBase, DeviceHandlerBase
from pyantonlib.channel import DefaultProtoChannel
from pyantonlib.utils import log_info, log_warn
from anton.plugin_pb2 import PipeType
from anton.device_pb2 import DEVICE_STATUS_ONLINE, DEVICE_KIND_STREAMING_STICK
from anton.device_pb2 import DEVICE_STATUS_OFFLINE
from anton.device_pb2 import DEVICE_KIND_SMART_SPEAKER, DEVICE_KIND_AUDIO_GROUP
from anton.media_pb2 import PLAYING, PAUSED, STOPPED
from anton.media_pb2 import VOLUME_UP, VOLUME_DOWN, VOLUME_MUTE
from anton.state_pb2 import DeviceState


class Channel(DefaultProtoChannel):
    pass


def get_device_id(obj):
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj.uuid)


def media_state_update(device_id, player_id, player_name, track_name, artist,
                       url, album_art_url, play_state):
    state = DeviceState(device_id=device_id)
    media_entry = state.media_state.add()
    if player_id:
        media_entry.player_id = player_id
    if player_name:
        media_entry.player_name = player_name
    if track_name:
        media_entry.track_name = track_name
    if artist:
        media_entry.artist = artist
    if url:
        media_entry.url = url
    if album_art_url:
        media_entry.album_art.url = album_art_url

    mapping = {"PLAYING": PLAYING, "PAUSED": PAUSED}
    play_status = mapping.get(play_state, STOPPED)
    media_entry.play_status = play_status

    return state


class ChromecastController(pychromecast.CastStatusListener):

    def __init__(self, device, device_handler):
        self.device = device
        self.device_id = get_device_id(device)
        self.device_handler = device_handler

        self.device_kind = {
            CAST_TYPE_CHROMECAST: DEVICE_KIND_STREAMING_STICK,
            CAST_TYPE_AUDIO: DEVICE_KIND_SMART_SPEAKER,
            CAST_TYPE_GROUP: DEVICE_KIND_AUDIO_GROUP,
        }.get(device.cast_type)

        self.latest_status = None

    def start(self):
        if self.device_kind not in (DEVICE_KIND_STREAMING_STICK,
                                    DEVICE_KIND_SMART_SPEAKER):
            return

        try:
            self.device.wait(timeout=2.0)
        except:
            log_warn("Unable to talk to Cast device: " + self.device.name)
            return

        self.device.register_status_listener(self)
        self.device.media_controller.register_status_listener(self)

        state = DeviceState(device_id=self.device_id,
                            friendly_name=self.device.name,
                            device_status=DEVICE_STATUS_ONLINE,
                            kind=self.device_kind)

        self.latest_status = self.device.status

        state.volume_state = int(self.device.status.volume_level * 100)

        capabilities = state.capabilities
        capabilities.media.supports_media = True
        capabilities.media.supports_volume_set = True
        capabilities.media.volume_controls.extend(
            [VOLUME_UP, VOLUME_DOWN, VOLUME_MUTE])
        player_cap = capabilities.media.player_capabilities.add()
        player_cap.player_id = self.device_id
        player_cap.supports_play_now = True
        player_cap.supports_play_next = True
        player_cap.supported_states.extend([PLAYING, PAUSED, STOPPED])

        self.device_handler.send_device_state_updated(state)

    def stop(self):
        try:
            self.device.disconnect(timeout=2.0)
        except:
            pass

        if self.device_kind not in (DEVICE_KIND_STREAMING_STICK,
                                    DEVICE_KIND_SMART_SPEAKER):
            return

        state = DeviceState(device_id=self.device_id,
                            device_status=DEVICE_STATUS_OFFLINE)
        self.device_handler.send_device_state_updated(state)

    def new_cast_status(self, status):
        log_info("New cast status: ", status)
        if self.latest_status.volume_level != status.volume_level:
            self.handle_volume_change(status)

        self.latest_status = status

    def handle_volume_change(self, status):
        state = DeviceState(device_id=self.device_id)
        state.volume_state = int(status.volume_level * 100)
        self.device_handler.send_device_state_updated(state)

    def new_media_status(self, status):
        media = self.device.media_controller

        player_id = str(self.device.uuid)
        player_name = (str(self.device.device.friendly_name)
                       if hasattr(self.device, "device")
                       and hasattr(self.device.device, "friendly_name") else
                       str(self.device.name))
        track_name = media.status.title
        artist = media.status.artist
        url = media.status.content_id or ""
        album_art_url = media.status.images[
            0].url if media.status.images else None
        play_state = media.status.player_state

        state = media_state_update(self.device_id, player_id, player_name,
                                   track_name, artist, url, album_art_url,
                                   play_state)
        self.device_handler.send_device_state_updated(state)

    def handle_media_instruction(self, media_instruction):
        mapping = {
            PLAYING: self.device.media_controller.play,
            PAUSED: self.device.media_controller.pause,
            STOPPED: self.device.media_controller.stop,
        }
        func = mapping.get(media_instruction.play_state_instruction)
        if not func:
            log_warn("Bad target play state.")
            return

        func()

    def handle_volume_instruction(self, volume_instruction):
        mapping = {
            VOLUME_UP: self.device.volume_up,
            VOLUME_DOWN: self.device.volume_down,
            VOLUME_MUTE: lambda: self.device.set_volume_muted(True),
        }
        func = mapping.get(volume_instruction)
        if not func:
            log_warn("Bad volume command.")
            return

        func()

    def handle_playlist_instruction(self, playlist_instruction):
        instruction_type = playlist_instruction.WhichOneof('type')
        if instruction_type == 'next_track':
            self.device.media_controller.skip()
        elif instruction_type == 'previous_track':
            self.device.media_controller.rewind()
        elif instruction_type == 'play_now':
            self.device.media_controller.play_media(
                playlist_instruction.play_now, "audio/mp3")
        else:
            log_warn("Playlist instruction type not supported: " +
                     str(instruction_type))

    def handle_set_device_state(self, msg, responder):
        if msg.volume_state > 0:
            self.device.set_volume(msg.volume_state / 100.0)
        if msg.volume_instruction:
            self.handle_volume_instruction(msg.volume_instruction)
        if msg.HasField('media_instruction'):
            self.handle_media_instruction(msg.media_instruction)
        if msg.HasField('playlist_instruction'):
            self.handle_playlist_instruction(msg.playlist_instruction)


class CastDevicesController(DeviceHandlerBase):

    def __init__(self):
        super().__init__()
        self.devices = {}

        self.zconf = zeroconf.Zeroconf()
        self.browser = pychromecast.CastBrowser(
            pychromecast.SimpleCastListener(self.on_cast_added,
                                            self.on_cast_removed), self.zconf)

    def start(self):
        log_info("Discovering Cast devices..")
        self.browser.start_discovery()

    def stop(self):
        self.browser.stop_discovery()

    def on_cast_added(self, uuid, service):
        cast_info = self.browser.devices[uuid]
        device = pychromecast.get_chromecast_from_cast_info(
            cast_info, self.zconf)
        log_info("Found a cast device:", device)
        controller = ChromecastController(device, self)
        self.devices[get_device_id(device)] = controller
        controller.start()

    def on_cast_removed(self, uuid, service, cast_info):
        controller = self.devices.pop(get_device_id(uuid))
        if not controller:
            return

        controller.stop()

    def handle_set_device_state(self, msg, responder):
        log_info("Handling set_device_state: " + str(msg))

        device_controller = self.devices.get(msg.device_id)
        if device_controller is None:
            raise ResourceNotFound(msg.device_id)
        device_controller.handle_set_device_state(msg, responder)


class AntonGoogleHomePlugin(AntonPlugin):

    def setup(self, plugin_startup_info):
        self.devices_handler = CastDevicesController()
        self.app_handler = AppHandlerBase(plugin_startup_info)

        self.channel = Channel(self.devices_handler, self.app_handler)

        registry = self.channel_registrar()
        registry.register_controller(PipeType.DEFAULT, self.channel)

    def on_start(self):
        self.devices_handler.start()

    def on_stop(self):
        self.devices_handler.stop()
