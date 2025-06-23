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


def media_event(device_id, player_id, player_name, track_name, artist, url,
                album_art, play_state):
    event = GenericEvent(device_id=device_id)
    if player_id:
        event.media.media.player_id = player_id
    if player_name:
        event.media.media.player_name = player_name
    if track_name:
        event.media.media.track_name = track_name
    if artist:
        event.media.media.artist = artist
    if url:
        event.media.media.url = url
    if album_art:
        event.media.media.album_art = album_art

    mapping = {"PLAYING": PLAYING, "PAUSED": PAUSED}
    play_status = mapping.get(play_state, STOPPED)
    event.media.media.play_status = play_status

    return event


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

        state = DeviceState(device_id=self.device_id,
                            friendly_name=self.device.name,
                            device_status=DEVICE_STATUS_ONLINE,
                            kind=self.device_kind)

        self.latest_status = self.device.status

        state.volume_state = int(self.device.status.volume_level * 100)

        capabilities = state.capabilities
        capabilities.media.volume_controls[:] = [
            VOLUME_UP, VOLUME_DOWN, VOLUME_MUTE
        ]
        capabilities.media.supported_states[:] = [PLAYING, PAUSED, STOPPED]
        capabilities.media.url_patterns[:] = []

        self.device_handler.send_device_state_updated(state)

    def stop(self):
        try:
            self.device.disconnect(timeout=2.0)
        except:
            pass

        if self.device_kind not in (CAST_TYPE_CHROMECAST, CAST_TYPE_AUDIO):
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
        player_name = str(self.device.device.friendly_name)
        track_name = media.status.title
        artist = media.status.artist
        url = "(no URL)"
        album_art = media.status.images and media.status.images[0].url
        play_state = media.status.player_state

        event = media_event(get_device_id(self.device), player_id, player_name,
                            track_name, artist, url, album_art, play_state)
        self.send_event(event)

    def handle_media_instruction(self, instruction):
        media = instruction.media
        oneof = media.WhichOneof('Instruction')

        if oneof == 'play_state_instruction':
            mapping = {
                PLAYING: self.device.media_controller.play,
                PAUSED: self.device.media_controller.pause
            }
            func = mapping.get(media.play_state_instruction)
            if not func:
                log_warn("Bad target play state.")
                return

            func()
        elif oneof ==  'volume':
            mapping = {
                VOLUME_UP: self.device.volume_up,
                VOLUME_DOWN: self.device.volume_down,
                VOLUME_MUTE: lambda: self.device.set_volume_muted(True),
                VOLUME_SET: lambda: self.device.set_volume(media.volume.level /
                                                           100.0)
            }
            func = mapping.get(media.volume.type)
            if not func:
                log_warn("Bad volume command.")
                return
            func()
        else:
            log_warn("Instruction handler not implemented.")

    def handle_set_device_state(self, msg, responder):
        if msg.volume_state > 0:
            self.device.set_volume(msg.volume_state / 100.0)


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
