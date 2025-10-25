# client/screen_client.py
"""
Screen Client - Captures and streams the screen using TCP.
Optimized for Windows using MSS for high-speed capture.
[FIXED] Added robust logging and traceback to find capture errors.
"""
import socket
import threading
import struct
import json
import time
import cv2
import numpy as np
import sys
import os
import zlib # Using zlib for compression instead of JPEG for speed
import traceback # <--- IMPORT TRACEBACK
from mss import mss # <--- Windows-specific high-speed library

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.config import *

# Fallback defaults for optional config values
if "SCREEN_QUALITY" not in globals():
    SCREEN_QUALITY = 80  # JPEG quality (0-100)

# Provide fallback for SCREEN_SHARE_RES used for capture (width, height)
# Default to 1280x720 (HD) if not defined in the imported config.
if "SCREEN_SHARE_RES" not in globals():
    SCREEN_SHARE_RES = (1280, 720)

class ScreenClient:
    def __init__(self, client_id, server_ip, status_callback=None):
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = SCREEN_PORT
        
        self.sock = None
        self.running = False
        self.is_presenting = False
        self.status_callback = status_callback
        
        self.receive_thread = None
        self.capture_thread = None
        
        self.current_frame = None
        self.frame_lock = threading.Lock()
        
        self.last_presenter_id = None
        self.last_status_time = 0

    def connect(self, **kwargs):
        """Connect to the screen share server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
            
            self.sock.settimeout(10.0) # Connection timeout
            self.sock.connect((self.server_ip, self.server_port))

            # Handshake: [4:client_id]
            handshake = struct.pack('!I', self.client_id)
            self.sock.sendall(handshake)
            
            # Receive acknowledgment [1:status_code]
            ack = self._recv_exact(1) # Use the fixed _recv_exact
            if ack != b'\x01':
                print("[SCREEN CLIENT ERROR] Server rejected connection.")
                self.sock.close()
                return False

            self.running = True
            # Set socket to blocking for the main loops
            self.sock.settimeout(None) 
            
            self.receive_thread = threading.Thread(target=self._receive_stream, daemon=True)
            self.receive_thread.start()
            
            print(f"[SCREEN CLIENT] Connected to {self.server_ip}:{self.server_port}")
            return True
            
        except Exception as e:
            print(f"[SCREEN CLIENT ERROR] Connection failed: {e}")
            traceback.print_exc() # <--- ADDED TRACEBACK
            if self.sock: self.sock.close()
            return False

    def start_sharing(self):
        """Tell the server we are starting to share."""
        if self.is_presenting: return
        try:
            # Send 'START' command [1:cmd_code]
            cmd_packet = struct.pack('!B', 1)
            self.sock.sendall(cmd_packet)
            
            self.is_presenting = True
            self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self.capture_thread.start()
            print("[SCREEN CLIENT] Started screen sharing.")
        except Exception as e:
            print(f"[SCREEN CLIENT ERROR] Failed to start sharing: {e}")
            traceback.print_exc() # <--- ADDED TRACEBACK

    def stop_sharing(self):
        """Tell the server we are stopping."""
        if not self.is_presenting: return
        self.is_presenting = False # Signal capture loop to stop
        if self.capture_thread:
            self.capture_thread.join(timeout=0.5)
        try:
            # Send 'STOP' command [1:cmd_code]
            cmd_packet = struct.pack('!B', 2)
            self.sock.sendall(cmd_packet)
            print("[SCREEN CLIENT] Stopped screen sharing.")
        except Exception as e:
            print(f"[SCREEN CLIENT ERROR] Failed to send stop command: {e}")
            traceback.print_exc() # <--- ADDED TRACEBACK

    def _capture_loop(self):
        """Captures the screen and sends frames (Windows/MSS optimized)."""
        frame_interval = 1.0 / SCREEN_FPS
        
        with mss() as sct:
            monitor = sct.monitors[1] # Grab the primary monitor
            
            # Calculate target size (e.g., 720p)
            width, height = SCREEN_SHARE_RES
            
            # Create a rect for capturing
            rect = {
                "top": monitor["top"],
                "left": monitor["left"],
                "width": monitor["width"],
                "height": monitor["height"],
            }
            
            while self.running and self.is_presenting:
                start_time = time.time()
                try:
                    # 1. Capture the screen
                    img_bgra = sct.grab(rect) # Returns BGRA
                    if not img_bgra:
                        print("[SCREEN CLIENT WARN] Capture failed: mss.grab() returned None.")
                        time.sleep(0.5)
                        continue

                    # 2. Convert to BGR numpy array
                    img_bgr = np.array(img_bgra, dtype=np.uint8)[:,:,:3] # Drop alpha
                    if img_bgr is None or img_bgr.size == 0:
                        print("[SCREEN CLIENT WARN] Capture failed: np.array conversion resulted in empty frame.")
                        time.sleep(0.5)
                        continue

                    # 3. Resize to target resolution
                    frame = cv2.resize(img_bgr, (width, height), interpolation=cv2.INTER_AREA)
                    if frame is None or frame.size == 0:
                        print("[SCREEN CLIENT WARN] Capture failed: cv2.resize resulted in empty frame.")
                        time.sleep(0.5)
                        continue

                    # 4. Encode as JPEG
                    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), SCREEN_QUALITY]
                    ret, buffer = cv2.imencode('.jpg', frame, encode_param)
                    if not ret:
                        print("[SCREEN CLIENT WARN] Capture failed: cv2.imencode failed.")
                        time.sleep(0.5)
                        continue

                    # 5. Send the frame data
                    frame_data = buffer.tobytes()
                    # Packet: [1:cmd_code=3 'FRAME'] [4:data_len] [data]
                    packet = struct.pack('!BI', 3, len(frame_data)) + frame_data
                    self.sock.sendall(packet)

                except (cv2.error, mss.exception.ScreenShotError) as e:
                    print(f"[SCREEN CLIENT WARN] Capture error (cv2/mss): {e}")
                    traceback.print_exc() # <--- PRINT FULL TRACEBACK
                    
                except Exception as e:
                    print(f"[SCREEN CLIENT ERROR] Capture loop error (General): {e}")
                    traceback.print_exc() # <--- PRINT FULL TRACEBACK
                    self.is_presenting = False # Stop on major error
                    break # Exit loop
                
                elapsed = time.time() - start_time
                if (sleep_time := frame_interval - elapsed) > 0:
                    time.sleep(sleep_time)

    def _receive_stream(self):
        """Receives status updates and frame data from the server."""
        while self.running:
            try:
                # This call now blocks until 1 byte is received, as intended
                header = self._recv_exact(1) 
                if not header:
                    print("[SCREEN CLIENT] Server disconnected.")
                    break
                
                msg_type = header[0]
                
                if msg_type == 10: # Status Update
                    # Read the presenter ID (4 bytes)
                    id_data = self._recv_exact(4)
                    if not id_data: break
                    
                    presenter_id = struct.unpack('!I', id_data)[0]
                    if presenter_id == 0: presenter_id = None # 0 means no one
                    
                    # Update GUI via callback
                    if presenter_id != self.last_presenter_id:
                        self.last_presenter_id = presenter_id
                        if self.status_callback:
                            self.status_callback({"presenter_id": presenter_id})
                    
                    # Clear local frame if presenter stopped
                    if presenter_id is None:
                        with self.frame_lock:
                            self.current_frame = None

                elif msg_type == 11: # Frame Data
                    # Read length (4 bytes)
                    len_data = self._recv_exact(4)
                    if not len_data: break
                    
                    frame_len = struct.unpack('!I', len_data)[0]
                    
                    # Read the full frame
                    frame_data = self._recv_exact(frame_len)
                    if not frame_data: break
                    
                    with self.frame_lock:
                        # Store the raw JPEG bytes
                        self.current_frame = frame_data 

            except Exception as e:
                if self.running:
                    print(f"[SCREEN CLIENT ERROR] Receive stream error: {e}")
                    traceback.print_exc() # <--- ADDED TRACEBACK
                break
        
        # Cleanup
        self.running = False
        if self.status_callback:
            self.status_callback({"presenter_id": None})
        print("[SCREEN CLIENT] Receive loop stopped.")

    def get_frame(self):
        """Called by GUI to get the current frame (raw JPEG bytes)."""
        if self.is_presenting: # Don't show your own stream
            return 'EMPTY'
            
        with self.frame_lock:
            if self.current_frame:
                frame_data = self.current_frame
                self.current_frame = None # Consume the frame
                return frame_data
            elif self.last_presenter_id is not None:
                return 'EMPTY' # Presenting, but no new frame
            else:
                return None # No one is presenting

    def _recv_exact(self, length):
        """
        Receive exactly 'length' bytes or return None on failure.
        This relies on the socket being in blocking mode (timeout=None).
        """
        if not self.sock: return None
        data = b''
        try:
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk: # Socket closed gracefully by peer
                    return None
                data += chunk
            return data
        except (socket.timeout, ConnectionError, OSError) as e:
            # Includes ConnectionResetError, ConnectionAbortedError
            print(f"[SCREEN CLIENT] _recv_exact failed: {e}")
            return None # Treat timeout/errors as connection failure for recv

    def disconnect(self):
        """Disconnect from the server and clean up resources."""
        print("[SCREEN CLIENT] Disconnecting...")
        self.running = False
        self.is_presenting = False # Stop capture loop

        if self.capture_thread:
            self.capture_thread.join(timeout=0.2)
        
        sock = self.sock
        self.sock = None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        if self.receive_thread:
            self.receive_thread.join(timeout=0.2)
            
        print("[SCREEN CLIENT] Disconnected.")