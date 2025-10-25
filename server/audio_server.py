# server/audio_server.py
"""
Audio Server - Receives audio streams, mixes them, and broadcasts.
Handles HELLO, AUDIO, and HEARTBEAT packets to manage client state.
"""

import socket
import threading
import struct
import time
import numpy as np
from collections import defaultdict, deque
import sys
sys.path.append('..')
from utils.config import *

class AudioServer:
    def __init__(self, host=SERVER_HOST, port=AUDIO_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.clients = {}  # {client_id: (ip, port)}
        self.client_lock = threading.Lock()
        self.running = False
        
        self.audio_buffers = defaultdict(lambda: deque(maxlen=10))
        self.buffer_lock = threading.Lock()
        
        self.stats = defaultdict(lambda: {'packets': 0, 'bytes': 0, 'last_seen': time.time()})
        
    def start(self):
        """Starts all server threads."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.host, self.port))
            self.running = True
            print(f"[AUDIO SERVER] Started on {self.host}:{self.port}")
            
            threading.Thread(target=self._receive_loop, daemon=True).start()
            threading.Thread(target=self._mix_and_broadcast_loop, daemon=True).start()
            threading.Thread(target=self._cleanup_inactive_clients_loop, daemon=True).start()
            
            return True
        except Exception as e:
            print(f"[AUDIO SERVER ERROR] Failed to start: {e}")
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
                    print(f"[AUDIO SERVER] Client {client_id} registered from {addr}")
                
                elif msg_type == 254: # HEARTBEAT
                    # This is just a keep-alive, no further action needed
                    pass
                
                elif msg_type == 2: # AUDIO
                    self._handle_audio_packet(client_id, data)

            except Exception as e:
                if self.running:
                    print(f"[AUDIO SERVER ERROR] Receive error: {e}")
    
    def _handle_audio_packet(self, client_id, data):
        """Processes an audio data packet."""
        if len(data) < 9: return # Header: [B:type][I:client_id][I:seq]
        
        audio_data = data[9:]
        if len(audio_data) != AUDIO_CHUNK * AUDIO_CHANNELS * 2:
            return # Ignore malformed packets

        with self.buffer_lock:
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
            self.audio_buffers[client_id].append(audio_array)
        
        with self.client_lock:
            self.stats[client_id]['packets'] += 1
            self.stats[client_id]['bytes'] += len(data)

    def _mix_and_broadcast_loop(self):
        """Periodically mixes audio and broadcasts to each client."""
        mix_interval = AUDIO_CHUNK / AUDIO_RATE  # Approx 0.02 seconds
        
        while self.running:
            start_time = time.time()
            
            # Get one audio chunk from each client that has data
            active_chunks = {}
            with self.buffer_lock:
                for cid, buffer in self.audio_buffers.items():
                    if buffer:
                        active_chunks[cid] = buffer.popleft()
            
            if not active_chunks:
                time.sleep(mix_interval / 2)
                continue

            self._broadcast_mixes(active_chunks)
            
            # Sleep to maintain the correct mixing interval
            elapsed = time.time() - start_time
            sleep_time = mix_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _broadcast_mixes(self, active_chunks):
        """Creates and sends a custom audio mix to each connected client."""
        with self.client_lock:
            if not self.clients: return
            
            # Loop through each client who should receive a mix
            for recipient_id, addr in self.clients.items():
                
                # Collect audio chunks from all OTHER clients
                mix_sources = [
                    audio for sender_id, audio in active_chunks.items() 
                    if sender_id != recipient_id
                ]
                
                if not mix_sources:
                    continue # Nothing to send to this client

                if len(mix_sources) == 1:
                    mixed_audio = mix_sources[0]
                else:
                    mixed_sum = np.sum([chunk.astype(np.int32) for chunk in mix_sources], axis=0)
                    mixed_audio = (mixed_sum // len(mix_sources)).astype(np.int16)

                # Create packet: [B:type][I:server_id=0][I:seq=0] + audio
                packet = struct.pack('!BII', 2, 0, 0) + mixed_audio.tobytes()
                
                try:
                    self.sock.sendto(packet, addr)
                except Exception as e:
                    print(f"[AUDIO SERVER] Failed to send mix to {recipient_id}: {e}")

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
                    print(f"[AUDIO SERVER] Removing inactive client {cid}")
                    del self.clients[cid]
                    del self.stats[cid]
                    with self.buffer_lock:
                        if cid in self.audio_buffers:
                            del self.audio_buffers[cid]

    # --- METHOD ADDED BACK ---
    def get_stats(self):
        """Return server statistics."""
        with self.client_lock:
            return {
                'active_clients': len(self.clients),
                'client_stats': dict(self.stats),
                'buffer_sizes': {cid: len(buf) for cid, buf in self.audio_buffers.items()}
            }
            
    def stop(self):
        """Stops the audio server."""
        print("[AUDIO SERVER] Stopping...")
        self.running = False
        if self.sock:
            self.sock.close()
        print("[AUDIO SERVER] Stopped")