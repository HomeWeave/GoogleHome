import os
from threading import Thread, Event

import pychromecast

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import GenericInstructionController
from pyantonlib.channel import GenericEventController
from pyantonlib.utils import log_info
from anton.plugin_pb2 import PipeType, IOT_INSTRUCTION, IOT_EVENTS
from anton.events_pb2 import GenericEvent
from anton.device_pb2 import DEVICE_STATUS_ONLINE, DEVICE_KIND_STREAMING_STICK
from anton.power_pb2 import POWER_OFF
from anton.media_pb2 import PLAYING, PAUSED, STOPPED


def online_event(device_id, friendly_name):
    event = GenericEvent(device_id=device_id)
    event.device.friendly_name = friendly_name
    event.device.device_kind = DEVICE_KIND_STREAMING_STICK
    event.device.device_status = DEVICE_STATUS_ONLINE

    capabilities = event.device.capabilities
    capabilities.power_state.supported_power_states[:] = [POWER_OFF]
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
        self.device.media_controller.register_status_listener(self)

    def new_media_status(self, status):
        media = self.device.media_controller

        player_id = str(self.device.uuid)
        player_name = str(self.device.device.friendly_name)
        track_name = media.status.title
        artist = media.status.artist
        url = "(no URL)"
        album_art = media.status.images and media.status.images[0].url
        play_state = media.status.player_state

        event = media_event(str(self.device.uuid), player_id, player_name,
                            track_name, artist, url, album_art, play_state)
        self.send_event(event)



class AntonGoogleHomePlugin(AntonPlugin):
    def setup(self, plugin_startup_info):
        instruction_controller = GenericInstructionController({})
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
                device_id = str(device.uuid)
                if device_id in self.controllers:
                    continue

                device.wait()
                controller = ChromecastController(device, self.send_event)
                self.controllers[device_id] = controller

                event = online_event(device.model_name + str(device.uuid),
                                     device.device.friendly_name)
                self.send_event(event)

    def on_stop(self):
        self.discovery_thread.join()


    def on_response(self, call_status):
        print("Received response:", call_status)

