# server/video_server.py
"""
Video Server - Receives video streams and broadcasts to all other clients.
Handles HELLO, VIDEO, and HEARTBEAT packets to manage client state.
"""

import socket
import threading
import struct
import time
from collections import defaultdict
import sys
sys.path.append('..')
from utils.config import *

class VideoServer:
    def __init__(self, host=SERVER_HOST, port=VIDEO_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.clients = {}  # {client_id: (ip, port)}
        self.client_lock = threading.Lock()
        self.running = False
        self.stats = defaultdict(lambda: {'packets': 0, 'bytes': 0, 'last_seen': time.time()})
        
    def start(self):
        """Starts all server threads."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.host, self.port))
            self.running = True
            print(f"[VIDEO SERVER] Started on {self.host}:{self.port}")
            
            threading.Thread(target=self._receive_loop, daemon=True).start()
            threading.Thread(target=self._cleanup_inactive_clients_loop, daemon=True).start()
            
            return True
        except Exception as e:
            print(f"[VIDEO SERVER ERROR] Failed to start: {e}")
            return False
    
    def _receive_loop(self):
        """Main loop to receive and handle all incoming packets."""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(MAX_PACKET_SIZE)
                if len(data) < 5: continue

                msg_type = data[0]
                client_id = struct.unpack('!I', data[1:5])[0]

                # Always update client address and last_seen time on any valid packet
                with self.client_lock:
                    self.clients[client_id] = addr
                    self.stats[client_id]['last_seen'] = time.time()

                if msg_type == 255: # HELLO
                    print(f"[VIDEO SERVER] Client {client_id} registered from {addr}")
                
                elif msg_type == 254: # HEARTBEAT
                    # This is just a keep-alive, no further action needed
                    pass
                
                elif msg_type == 1: # VIDEO
                    self.stats[client_id]['packets'] += 1
                    self.stats[client_id]['bytes'] += len(data)
                    self._broadcast_packet(data, sender_id=client_id)

            except Exception as e:
                if self.running:
                    print(f"[VIDEO SERVER ERROR] Receive error: {e}")
    
    def _broadcast_packet(self, data, sender_id):
        """Broadcast a packet to all clients except the sender."""
        with self.client_lock:
            for cid, addr in self.clients.items():
                if cid != sender_id:
                    try:
                        self.sock.sendto(data, addr)
                    except Exception as e:
                        print(f"[VIDEO SERVER] Failed to send to client {cid}: {e}")
    
    def _cleanup_inactive_clients_loop(self):
        """Periodically remove clients that have timed out."""
        while self.running:
            time.sleep(CONNECTION_TIMEOUT / 2)
            current_time = time.time()
            
            with self.client_lock:
                inactive_ids = [
                    cid for cid, stats in self.stats.items()
                    if current_time - stats['last_seen'] > CONNECTION_TIMEOUT
                ]
                
                for cid in inactive_ids:
                    print(f"[VIDEO SERVER] Removing inactive client {cid}")
                    del self.clients[cid]
                    del self.stats[cid]
    
    # --- METHOD ADDED BACK ---
    def get_stats(self):
        """Return server statistics."""
        with self.client_lock:
            return {
                'active_clients': len(self.clients),
                'client_stats': dict(self.stats)
            }
    
    def stop(self):
        """Stops the video server."""
        print("[VIDEO SERVER] Stopping...")
        self.running = False
        if self.sock:
            self.sock.close()
        print("[VIDEO SERVER] Stopped")