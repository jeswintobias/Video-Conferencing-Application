# client/video_client.py
"""
Video Client - Captures webcam, sends to server, and displays remote streams.
Includes a heartbeat mechanism and a faster timeout for clearing frozen streams.
"""

import socket
import threading
import struct
import time
import cv2
import numpy as np
import sys
sys.path.append('..')
from utils.config import *

class VideoClient:
    def __init__(self, client_id, server_ip):
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = VIDEO_PORT
        
        self.sock = None
        self.running = False
        self.sending = False
        
        self.cap = None
        self.send_thread = None
        self.receive_thread = None
        self.heartbeat_thread = None
        
        self.video_streams = {}
        self.stream_lock = threading.Lock()
        
        self.stream_timestamps = {}
        # <<< FIX: Reduced timeout from 5.0 to 0.5 seconds for faster clearing
        self.stream_timeout = 0.5   
        
        self.seq_num = 0

    # ... (The rest of the file is identical to the previous correct version) ...
    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(1.0)
            self.running = True
            self.receive_thread = threading.Thread(target=self._receive_video, daemon=True)
            self.heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self.receive_thread.start()
            self.heartbeat_thread.start()
            self._send_hello_packet()
            print(f"[VIDEO CLIENT] Connected to {self.server_ip}:{self.server_port}")
            return True
        except Exception as e:
            print(f"[VIDEO CLIENT ERROR] Connection failed: {e}")
            self.running = False
            return False
    
    def start_camera(self, camera_index=0):
        if self.sending: return True
        try:
            self.cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW if sys.platform == 'win32' else -1)
            if not self.cap.isOpened(): raise IOError("Cannot open webcam")
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_WIDTH)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_HEIGHT)
            self.cap.set(cv2.CAP_PROP_FPS, VIDEO_FPS)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.sending = True
            self.send_thread = threading.Thread(target=self._send_video, daemon=True)
            self.send_thread.start()
            print("[VIDEO CLIENT] Camera started")
            return True
        except Exception as e:
            print(f"[VIDEO CLIENT ERROR] Camera start failed: {e}")
            self.cap = None
            return False

    def stop_camera(self):
        if not self.sending: return
        self.sending = False
        if self.send_thread: self.send_thread.join(timeout=0.5)
        if self.cap: self.cap.release()
        self.cap = None
        print("[VIDEO CLIENT] Camera stopped")
    
    def _send_hello_packet(self):
        try:
            packet = struct.pack('!BI', 255, self.client_id)
            for _ in range(3):
                self.sock.sendto(packet, (self.server_ip, self.server_port))
                time.sleep(0.01)
        except Exception as e:
            print(f"[VIDEO CLIENT ERROR] Failed to send HELLO packet: {e}")

    def _send_heartbeat(self):
        while self.running:
            try:
                time.sleep(10)
                if self.running and not self.sending:
                    packet = struct.pack('!BI', 254, self.client_id)
                    self.sock.sendto(packet, (self.server_ip, self.server_port))
            except Exception as e:
                if self.running: print(f"[VIDEO CLIENT ERROR] Heartbeat failed: {e}")

    def _send_video(self):
        frame_interval = 1.0 / VIDEO_FPS
        while self.running and self.sending:
            start_time = time.time()
            try:
                ret, frame = self.cap.read()
                if not ret:
                    print("[VIDEO CLIENT] Failed to capture frame, stopping camera.")
                    self.stop_camera()
                    break
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), VIDEO_QUALITY]
                _, buffer = cv2.imencode('.jpg', frame, encode_param)
                self._send_frame_chunks(buffer.tobytes())
                elapsed = time.time() - start_time
                if (sleep_time := frame_interval - elapsed) > 0:
                    time.sleep(sleep_time)
            except Exception as e:
                if self.running and self.sending: print(f"[VIDEO CLIENT ERROR] Send video error: {e}")
                break

    def _send_frame_chunks(self, frame_data):
        total_chunks = (len(frame_data) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for i in range(total_chunks):
            chunk_data = frame_data[i*CHUNK_SIZE:(i+1)*CHUNK_SIZE]
            packet = struct.pack('!BIIHH', 1, self.client_id, self.seq_num, i, total_chunks) + chunk_data
            self.sock.sendto(packet, (self.server_ip, self.server_port))
        self.seq_num += 1

    def _receive_video(self):
        frame_buffer = {}
        while self.running:
            try:
                data, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                if not data or len(data) < 5: continue
                msg_type = data[0]
                if msg_type == 1:
                    if len(data) < 13: continue
                    _, sender_id, seq, chunk_idx, total_chunks = struct.unpack('!BIIHH', data[:13])
                    if sender_id == self.client_id: continue
                    frame_key = (sender_id, seq)
                    if frame_key not in frame_buffer:
                        if len(frame_buffer) > 50:
                            del frame_buffer[min(frame_buffer.keys(), key=lambda k: k[1])]
                        frame_buffer[frame_key] = {}
                    frame_buffer[frame_key][chunk_idx] = data[13:]
                    if len(frame_buffer[frame_key]) == total_chunks:
                        frame_data = b''.join(frame_buffer[frame_key][i] for i in range(total_chunks))
                        frame = cv2.imdecode(np.frombuffer(frame_data, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            with self.stream_lock:
                                self.video_streams[sender_id] = frame
                                self.stream_timestamps[sender_id] = time.time()
                        del frame_buffer[frame_key]
            except socket.timeout:
                continue
            except Exception as e:
                if self.running: print(f"[VIDEO CLIENT ERROR] Receive error: {e}")

    def get_frames(self):
        with self.stream_lock:
            current_time = time.time()
            active_streams = {}
            # Use list() to allow deletion during iteration
            inactive_cids = []

            for cid, frame in self.video_streams.items():
                timestamp = self.stream_timestamps.get(cid)
                
                # Check if the timestamp exists and is recent
                if timestamp and (current_time - timestamp < self.stream_timeout):
                    active_streams[cid] = frame
                else:
                    # This stream is stale. Mark it for removal.
                    inactive_cids.append(cid)
            
            # Clean up the stale streams
            for cid in inactive_cids:
                if cid in self.video_streams:
                    del self.video_streams[cid]
                if cid in self.stream_timestamps:
                    del self.stream_timestamps[cid]
                    
            return active_streams
    
    def disconnect(self):
        print("[VIDEO CLIENT] Disconnecting...")
        self.running = False
        self.stop_camera()
        if self.heartbeat_thread: self.heartbeat_thread.join(timeout=0.5)
        if self.receive_thread: self.receive_thread.join(timeout=0.5)
        if self.sock: self.sock.close()
        print("[VIDEO CLIENT] Disconnected.")