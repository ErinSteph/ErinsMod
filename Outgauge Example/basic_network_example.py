# Streams JSON OutGauge data from ErinsMod on any network device.
# - Enable OutGauge API in CarX and load into a track
# - Run this script on another laptop or something
# - Car telemetry should log to the console.

import socket
import time

PORT = 9998

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

sock.bind(("0.0.0.0", PORT))
sock.settimeout(5)

print(f"Listening for UDP on 0.0.0.0:{PORT}")
print("Waiting for packets...\n")

while True:
    try:
        data, addr = sock.recvfrom(4096)
    except socket.timeout:
        print("...no packets yet")
        continue

    src_ip, src_port = addr
    print(f"\n--- Packet from {src_ip}:{src_port} ({len(data)} bytes) ---")

    try:
        text = data.decode("utf-8", "ignore")
        print(text)
    except Exception:
        print(data)
