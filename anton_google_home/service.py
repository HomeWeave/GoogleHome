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


def online_event(device_id, friendly_name):
    event = GenericEvent(device_id=device_id)
    event.device.friendly_name = friendly_name
    event.device.device_kind = DEVICE_KIND_STREAMING_STICK
    event.device.device_status = DEVICE_STATUS_ONLINE

    capabilities = event.device.capabilities
    capabilities.power_state.supported_power_states[:] = [POWER_OFF]
    capabilities.media.url_patterns[:] = []

    return event



class AntonGoogleHomePlugin(AntonPlugin):
    def setup(self, plugin_startup_info):
        instruction_controller = GenericInstructionController({})
        event_controller = GenericEventController(lambda call_status: 0)
        self.send_event = event_controller.create_client(0, self.on_response)

        registry = self.channel_registrar()
        registry.register_controller(IOT_INSTRUCTION,
                                     instruction_controller)
        registry.register_controller(IOT_EVENTS, event_controller)

    def on_start(self):
        self.discovery_thread = Thread(target=self.discover_chromecasts)
        self.discovery_thread.start()

    def discover_chromecasts(self):
        devices, browser = pychromecast.get_chromecasts()
        browser.stop_discovery()

        for device in devices:
            event = online_event(device.model_name + str(device.uuid),
                                 device.device.friendly_name)
            self.send_event(event)

    def on_stop(self):
        self.discovery_thread.join()


    def on_response(self, call_status):
        print("Received response:", call_status)

