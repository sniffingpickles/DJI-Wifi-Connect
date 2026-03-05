"""DJI Pocket 3 Control Suite — educational camera control project.

Modules:
    ble            BLE scanning, pairing, WiFi AP activation
    wifi           WiFi network joining, camera reachability
    udp_protocol   DJI UDP protocol (handshake, packets, DUML transport)
    duml           DUML packet building/parsing
    video          H.264 video receiver, ffplay pipe, file recording
    gimbal         PTZ gimbal control and attitude feedback
    camera         Camera status, heartbeats, photo/record commands
"""

__version__ = "0.1.0"
