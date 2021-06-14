import os
from threading import Thread, Event

import pychromecast
from pychromecast.const import CAST_TYPE_CHROMECAST, CAST_TYPE_AUDIO
from pychromecast.const import CAST_TYPE_GROUP

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import GenericInstructionController
from pyantonlib.channel import GenericEventController
from pyantonlib.utils import log_info, log_warn
from anton.plugin_pb2 import PipeType, IOT_INSTRUCTION, IOT_EVENTS
from anton.events_pb2 import GenericEvent
from anton.device_pb2 import DEVICE_STATUS_ONLINE, DEVICE_KIND_STREAMING_STICK
from anton.device_pb2 import DEVICE_KIND_SMART_SPEAKER, DEVICE_KIND_AUDIO_GROUP
from anton.media_pb2 import PLAYING, PAUSED, STOPPED
from anton.media_pb2 import VOLUME_UP, VOLUME_DOWN, VOLUME_SET, VOLUME_MUTE


def get_device_id(device):
    return device.model_name + str(device.uuid)


def online_event(device_id, friendly_name, device_kind):
    event = GenericEvent(device_id=device_id)
    event.device.friendly_name = friendly_name
    event.device.device_kind = device_kind
    event.device.device_status = DEVICE_STATUS_ONLINE


    capabilities = event.device.capabilities
    capabilities.media.volume_controls[:] = [VOLUME_UP, VOLUME_DOWN,
                                             VOLUME_MUTE, VOLUME_SET]
    capabilities.media.url_patterns[:] = []

    return event


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


class ChromecastController(object):
    def __init__(self, device, send_event):
        self.device = device
        self.send_event = send_event

        self.device_kind = {
            CAST_TYPE_CHROMECAST: DEVICE_KIND_STREAMING_STICK,
            CAST_TYPE_AUDIO: DEVICE_KIND_SMART_SPEAKER,
            CAST_TYPE_GROUP: DEVICE_KIND_AUDIO_GROUP,
        }.get(device.device.cast_type)

        if not self.device_kind:
            return

        self.device.media_controller.register_status_listener(self)

        event = online_event(get_device_id(device), device.device.friendly_name,
                             self.device_kind)
        self.send_event(event)

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


class AntonGoogleHomePlugin(AntonPlugin):
    def setup(self, plugin_startup_info):
        instruction_controller = GenericInstructionController({
            "media": self.handle_media_instruction
        })
        event_controller = GenericEventController(lambda call_status: 0)
        self.send_event = event_controller.create_client(0, self.on_response)

        registry = self.channel_registrar()
        registry.register_controller(IOT_INSTRUCTION, instruction_controller)
        registry.register_controller(IOT_EVENTS, event_controller)

        self.controllers = {}

    def on_start(self):
        self.discovery_thread = Thread(target=self.discover_chromecasts)
        self.discovery_thread.start()

    def discover_chromecasts(self):
        while True:
            devices, browser = pychromecast.get_chromecasts()
            browser.stop_discovery()

            for device in devices:
                device_id = get_device_id(device)
                if device_id in self.controllers:
                    continue

                device.wait()

                controller = ChromecastController(device, self.send_event)
                self.controllers[device_id] = controller

    def handle_media_instruction(self, instruction):
        device_id = instruction.device_id
        controller = self.controllers.get(device_id)

        if not controller:
            log_warn("No controller found for device ID: " + device_id)
            return

        controller.handle_media_instruction(instruction)


    def on_stop(self):
        self.discovery_thread.join()


    def on_response(self, call_status):
        print("Received response:", call_status)

