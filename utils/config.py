# utils/config.py
"""
Final, consolidated configuration settings for the LAN Communication System.
"""

# --- Server Configuration ---
SERVER_HOST = '0.0.0.0'  # Listen on all available network interfaces
VIDEO_PORT = 5000
AUDIO_PORT = 5001
CHAT_PORT = 5002
FILE_PORT = 5003
SCREEN_PORT = 5005 # Final screen sharing port

# --- Network Configuration ---
MAX_PACKET_SIZE = 65507  # Max safe UDP packet size
BUFFER_SIZE = 262144     # 256KB socket buffer size
MAX_CLIENTS = 16
CONNECTION_TIMEOUT = 35  # Seconds before a client is considered disconnected

# --- Video Configuration ---
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
VIDEO_FPS = 15           # A stable frame rate for typical LANs
VIDEO_QUALITY = 75       # A good balance of quality and size
CHUNK_SIZE = 60000       # Video chunk size, must be less than MAX_PACKET_SIZE

# --- Audio Configuration ---
AUDIO_RATE = 48000       # Standard sample rate
AUDIO_CHANNELS = 1       # Mono audio is sufficient for voice
AUDIO_CHUNK = 960        # Creates 20ms audio packets (48000 / 960 = 50 packets/sec)

# --- Screen Sharing Configuration ---
SCREEN_FPS = 10          # Frame rate for screen sharing