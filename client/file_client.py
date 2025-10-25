"""
File Client - Uploads and downloads files via TCP
[FIXED] Heartbeat mechanism to keep connections alive
[FIXED] Centralized connection state management to avoid deadlocks
[FIXED] Auto-reconnection support
[FIXED] connect() race condition
[CLEANUP] Removed redundant OS-level TCP Keepalives
"""

import socket
import threading
import struct
import json
import time
import os
import hashlib
from queue import Queue, Empty
import sys
import traceback

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.config import *

class FileClient:
    def __init__(self, client_id, server_ip, username):
        self.client_id = client_id
        self.username = username
        self.server_ip = server_ip
        self.server_port = FILE_PORT
        self.sock = None
        self.running = False
        self.connected = False
        self.available_files = []
        self.file_lock = threading.Lock()
        self.socket_lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.new_file_callbacks = []
        self.progress_callbacks = []
        self.connection_status_callbacks = []
        self.listen_thread = None
        self.heartbeat_thread = None
        self.last_heartbeat_time = 0
        self.heartbeat_interval = 15
        self.heartbeat_timeout = 5
        self.download_dir = "client_downloads"
        os.makedirs(self.download_dir, exist_ok=True)
        self.files_uploaded = 0
        self.files_downloaded = 0
        self.bytes_uploaded = 0
        self.bytes_downloaded = 0
        self.auto_reconnect = False
        self.reconnect_delay = 5

    def connect(self, auto_reconnect=False):
        """
        Connect to the file server.
        [FIX] This entire method is now inside the connection_lock
        to prevent a race condition.
        """
        
        with self.connection_lock:
            if self.connected:
                print("[FILE CLIENT] Already connected.")
                return True
            
            # If we are already running (e.g., in a reconnect loop),
            # this check prevents starting another connect attempt.
            if self.running and not auto_reconnect:
                 print("[FILE CLIENT] Connection attempt already in progress.")
                 return False

            self.auto_reconnect = auto_reconnect
            self.running = True # Mark as "attempting to run"
            
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                
                # [CLEANUP] Removed redundant SO_KEEPALIVE socket options.
                # The application-level PING/PONG is superior.
                
                self.sock.settimeout(10.0) # Connection timeout
                
                print(f"[FILE CLIENT] Connecting to {self.server_ip}:{self.server_port}...")
                self.sock.connect((self.server_ip, self.server_port))
                
                username_bytes = self.username.encode('utf-8')
                if len(username_bytes) > 255:
                    username_bytes = username_bytes[:255]
                    print("[FILE CLIENT WARN] Username truncated to 255 bytes.")
                
                handshake = struct.pack('!IB', self.client_id, len(username_bytes)) + username_bytes
                self.sock.sendall(handshake)
                
                self.sock.settimeout(5.0) # Handshake ACK timeout
                ack = self._recv_exact(6)
                
                if ack and len(ack) == 6:
                    ack_id = struct.unpack('!I', ack[:4])[0]
                    ack_status = ack[4:]
                    
                    if ack_id == self.client_id and ack_status == b'OK':
                        self.connected = True # Set connected *before* starting threads
                        
                        # Set a long default timeout.
                        # Threads will override this as needed.
                        self.sock.settimeout(120.0) 
                        
                        # Receive initial file list (must be inside socket_lock)
                        with self.socket_lock:
                            if not self._receive_file_list(expected=True):
                                print("[FILE CLIENT ERROR] Failed to receive initial file list.")
                                self.connected = False
                                self.running = False
                                try: self.sock.close()
                                except: pass
                                self.sock = None
                                return False
                        
                        # Start listener thread
                        self.listen_thread = threading.Thread(target=self._listen_notifications, daemon=True)
                        self.listen_thread.start()
                        
                        # Start heartbeat thread
                        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                        self.heartbeat_thread.start()
                        self.last_heartbeat_time = time.time()
                        
                        print(f"[FILE CLIENT] Connected to {self.server_ip}:{self.server_port}")
                        self._notify_connection_status(True)
                        return True
                    else:
                        print(f"[FILE CLIENT ERROR] Invalid ACK: ID={ack_id}, Status={ack_status}")
                else:
                    print(f"[FILE CLIENT ERROR] Failed to receive valid ACK")
                
                # Connection failed
                self.running = False
                self.connected = False
                if self.sock:
                    self.sock.close()
                self.sock = None
                return False
                
            except socket.gaierror as e:
                print(f"[FILE CLIENT ERROR] Address error: {e}")
            except socket.timeout:
                print(f"[FILE CLIENT ERROR] Connection timed out")
            except ConnectionRefusedError:
                print(f"[FILE CLIENT ERROR] Connection refused. Is server running?")
            except Exception as e:
                print(f"[FILE CLIENT ERROR] Connection failed: {e}")
                traceback.print_exc()

            # Cleanup on any exception
            self.running = False
            self.connected = False
            if self.sock:
                try: self.sock.close()
                except: pass
            self.sock = None
            return False

    def _heartbeat_loop(self):
        """
        Sends periodic heartbeats.
        This is now the *master* controller for connection state.
        It is the ONLY loop that triggers disconnects or reconnects.
        """
        print("[FILE CLIENT] Heartbeat monitor started")
        consecutive_failures = 0
        max_failures = 3
        
        while self.running:
            try:
                # Wait for the interval
                # We check self.running in smaller increments
                # so the thread can shut down faster.
                for _ in range(self.heartbeat_interval):
                    if not self.running:
                        break
                    time.sleep(1)
                
                if not self.running:
                    break
                
                with self.connection_lock:
                    if not self.connected:
                        break
                
                # Try to send a heartbeat
                success = self._send_heartbeat()
                
                if success:
                    consecutive_failures = 0
                    self.last_heartbeat_time = time.time()
                    # print("[FILE CLIENT DEBUG] Heartbeat OK")
                else:
                    consecutive_failures += 1
                    print(f"[FILE CLIENT WARN] Heartbeat failed ({consecutive_failures}/{max_failures})")
                    
                    if consecutive_failures >= max_failures:
                        print("[FILE CLIENT ERROR] Multiple heartbeat failures - connection lost")
                        
                        # This is the *only* place a network failure
                        # leads to a state change.
                        with self.connection_lock:
                            self.connected = False
                        self._notify_connection_status(False)
                        
                        if self.auto_reconnect and self.running:
                            # This thread will exit after _attempt_reconnect
                            # starts the new connection process.
                            self._attempt_reconnect()
                        else:
                            # Not auto-reconnecting, just shut down
                            self.disconnect()
                        
                        break # Exit the heartbeat loop
                        
            except Exception as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Heartbeat loop error: {e}")
                    traceback.print_exc()
                time.sleep(1)
        
        print("[FILE CLIENT] Heartbeat monitor stopped")

    def _send_heartbeat(self):
        """Send a heartbeat (ping) and wait for pong response."""
        
        # Check connection state *before* acquiring socket lock
        # to avoid deadlock
        with self.connection_lock:
            if not self.connected or not self.sock:
                return False
                
        try:
            with self.socket_lock:
                # Check again *inside* socket lock
                if not self.sock: return False

                original_timeout = self.sock.gettimeout()
                self.sock.settimeout(self.heartbeat_timeout)
                
                # Send TYPE 5 (PING)
                self.sock.sendall(b'\x05')
                
                # Wait for TYPE 6 (PONG)
                response = self._recv_exact(1)
                
                self.sock.settimeout(original_timeout)
                
                if response == b'\x06':
                    return True
                else:
                    print(f"[FILE CLIENT WARN] Unexpected heartbeat response: {response}")
                    return False
                    
        except socket.timeout:
            print("[FILE CLIENT WARN] Heartbeat timeout")
            return False
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Heartbeat connection error: {e}")
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Heartbeat unexpected error: {e}")
            return False

    def _attempt_reconnect(self):
        """Attempt to reconnect to the server."""
        print("[FILE CLIENT] Attempting auto-reconnect...")
        
        # Set running=False to stop other threads (like listener)
        # This thread (heartbeat) will exit when this func returns.
        with self.connection_lock:
            self.running = False
            self.connected = False
        
        # Clean up current connection
        sock = self.sock
        self.sock = None
        if sock:
            try: sock.close()
            except: pass
        
        # Wait for listener thread to die
        if self.listen_thread and self.listen_thread.is_alive():
            self.listen_thread.join(timeout=1.0)
            
        # Wait before reconnecting
        time.sleep(self.reconnect_delay)
        
        with self.connection_lock:
            # Check if a manual disconnect() happened while we were sleeping
            if not self.auto_reconnect:
                print("[FILE CLIENT] Auto-reconnect cancelled.")
                return
        
        # Try to reconnect
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            print(f"[FILE CLIENT] Reconnection attempt {attempt}/{max_attempts}...")
            
            # connect() will set self.running=True and start new threads
            if self.connect(auto_reconnect=True):
                print("[FILE CLIENT] Reconnection successful!")
                # Refresh file list after reconnection
                self.refresh_file_list()
                return
            
            with self.connection_lock:
                if not self.auto_reconnect:
                    print("[FILE CLIENT] Auto-reconnect cancelled during attempts.")
                    return

            if attempt < max_attempts:
                time.sleep(self.reconnect_delay)
        
        print("[FILE CLIENT] Reconnection failed after all attempts")
        self._notify_connection_status(False) # Already notified, but good to be sure

    def _receive_file_list(self, expected=False):
        """
        Receives and updates the list of available files.
        *** MUST BE CALLED FROM WITHIN self.socket_lock ***
        [FIX] This function NO LONGER changes connection state.
        """
        original_timeout = None
        try:
            if not self.sock:
                if expected: print("[FILE CLIENT ERROR] Socket is None")
                return False
            
            original_timeout = self.sock.gettimeout()
            self.sock.settimeout(10.0)
            
            list_len_data = self._recv_exact(4)
            if not list_len_data:
                if expected: print("[FILE CLIENT ERROR] Failed to receive file list length")
                return False
            
            list_len = struct.unpack('!I', list_len_data)[0]
            
            if list_len > 10 * 1024 * 1024: # 10MB limit
                print(f"[FILE CLIENT ERROR] File list too large: {list_len}")
                self.sock.settimeout(original_timeout)
                return False
            
            if list_len == 0:
                with self.file_lock:
                    self.available_files = []
                print("[FILE CLIENT] Received empty file list.")
                self.sock.settimeout(original_timeout)
                return True
            
            list_data = self._recv_exact(list_len)
            if not list_data:
                if expected: print("[FILE CLIENT ERROR] Failed to receive file list data")
                self.sock.settimeout(original_timeout)
                return False
            
            self.sock.settimeout(original_timeout)
            original_timeout = None # Flag that timeout was reset
            
            file_list = json.loads(list_data.decode('utf-8'))
            with self.file_lock:
                self.available_files = file_list
            
            print(f"[FILE CLIENT] Received file list: {len(file_list)} files available.")
            self.last_heartbeat_time = time.time()
            return True
            
        except (socket.timeout, ConnectionError, OSError) as e:
            if expected:
                print(f"[FILE CLIENT ERROR] Connection issue receiving file list: {e}")
            return False
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[FILE CLIENT ERROR] Failed to parse file list: {e}")
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected error receiving file list: {e}")
            traceback.print_exc()
            return False
        finally:
            if self.sock and original_timeout is not None:
                try: self.sock.settimeout(original_timeout)
                except: pass

    def _listen_notifications(self):
        """
        Listens for asynchronous notifications.
        [FIX] This thread NO LONGER changes connection state.
        It just exits on failure, letting the heartbeat loop take over.
        """
        print("[FILE CLIENT] Notification listener started")
        
        while self.running:
            try:
                type_data = None
                
                with self.socket_lock:
                    if not self.running:
                        break
                    if not self.sock:
                        break
                    
                    original_timeout = self.sock.gettimeout()
                    try:
                        self.sock.settimeout(1.0) # Poll every 1 second
                        type_data = self._recv_exact(1)
                        self.sock.settimeout(original_timeout)
                    except socket.timeout:
                        self.sock.settimeout(original_timeout)
                        continue # This is normal, just loop again
                    except Exception as e:
                        # Catch error *inside* lock to reset timeout
                        if self.running:
                            print(f"[FILE CLIENT DEBUG] Listener read error: {e}")
                        try: self.sock.settimeout(original_timeout)
                        except: pass
                        break # Exit loop on error
                
                # --- We are outside the socket_lock here ---
                
                if not type_data:
                    if self.running:
                        print("[FILE CLIENT INFO] Server disconnected (listener)")
                    break # Exit loop
                
                msg_type = type_data[0]
                
                # Re-acquire lock to process the rest of the message
                with self.socket_lock:
                    if not self.running or not self.sock:
                        break
                        
                    if msg_type == 6: # PONG
                        # Heartbeat loop handles this, but good to update time
                        self.last_heartbeat_time = time.time()
                        continue
                    
                    if msg_type == 4: # New File Notification
                        len_data = self._recv_exact(4)
                        if not len_data:
                            break # Connection lost
                        
                        notif_len = struct.unpack('!I', len_data)[0]
                        
                        if 0 < notif_len < 1024 * 10:
                            notif_data = self._recv_exact(notif_len)
                            if not notif_data:
                                break # Connection lost
                            
                            try:
                                notification = json.loads(notif_data.decode('utf-8'))
                                self._process_new_file_notification(notification)
                                self.last_heartbeat_time = time.time()
                            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                                print(f"[FILE CLIENT WARN] Invalid notification: {e}")
                        else:
                            print(f"[FILE CLIENT WARN] Invalid notification length: {notif_len}")
                    
                    elif msg_type == 3: # Unexpected file list
                        print("[FILE CLIENT WARN] Unexpected file list in listener")
                        if not self._receive_file_list(expected=False):
                            print("[FILE CLIENT WARN] Failed to process unexpected file list")
                            break # Connection lost
                    
                    else:
                        print(f"[FILE CLIENT WARN] Unknown message type: {msg_type}")
            
            except (ConnectionError, OSError) as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Connection error in listener: {e}")
                break # Exit loop
            except Exception as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Unexpected error in listener: {e}")
                    traceback.print_exc()
                break # Exit loop
        
        # This thread is stopping. Do not change global state.
        # The heartbeat loop will handle the disconnect.
        print("[FILE CLIENT] Notification listener stopped.")

    def _process_new_file_notification(self, notification):
        """Process new file notifications."""
        if notification.get('type') == 'new_file':
            file_info = {
                'file_id': notification.get('file_id'),
                'filename': notification.get('filename'),
                'filesize': notification.get('filesize'),
                'uploader': notification.get('uploader'),
                'timestamp': notification.get('timestamp')
            }
            
            if not all(k in file_info and file_info[k] is not None 
                       for k in ['file_id', 'filename', 'filesize', 'uploader']) or \
               not isinstance(file_info['filesize'], int):
                print(f"[FILE CLIENT WARN] Invalid notification: {notification}")
                return
            
            with self.file_lock:
                if not any(f['file_id'] == file_info['file_id'] for f in self.available_files):
                    self.available_files.append(file_info)
                    print(f"[FILE CLIENT] New file available: {file_info['filename']}")
                    self._notify_new_file_callbacks(file_info.copy())

    def upload_file(self, filepath):
        """
        Uploads a file to the server.
        [FIX] This function NO LONGER calls self.disconnect() on failure.
        """
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            print(f"[FILE CLIENT ERROR] File not found: {filepath}")
            filename = os.path.basename(filepath)
            self._notify_progress_callbacks('upload', filename, -1, 0, 0)
            return False
        
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT ERROR] Cannot upload: Not connected")
                filename = os.path.basename(filepath)
                self._notify_progress_callbacks('upload', filename, -1, 0, 0)
                return False
        
        filename = os.path.basename(filepath)
        filesize = 0
        total_sent = 0
        original_timeout = None
        
        try:
            filesize = os.path.getsize(filepath)
            
            if filesize > 1 * 1024 * 1024 * 1024: # 1GB limit
                print(f"[FILE CLIENT ERROR] File too large: {filesize} bytes")
                self._notify_progress_callbacks('upload', filename, -1, 0, filesize)
                return False
            
            print(f"[FILE CLIENT] Starting upload for '{filename}' ({filesize} bytes)...")
            self._notify_progress_callbacks('upload', filename, 0, 0, filesize)
            
            file_hash = hashlib.sha256()
            try:
                with open(filepath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        file_hash.update(chunk)
                file_hash_hex = file_hash.hexdigest()
            except IOError as e:
                print(f"[FILE CLIENT ERROR] Could not read file: {e}")
                self._notify_progress_callbacks('upload', filename, -1, 0, filesize)
                return False
            
            metadata = {'filename': filename, 'filesize': filesize, 'hash': file_hash_hex}
            meta_json = json.dumps(metadata).encode('utf-8')
            
            with self.socket_lock:
                with self.connection_lock:
                    if not self.connected or not self.sock:
                        print("[FILE CLIENT ERROR] Cannot upload: Not connected")
                        self._notify_progress_callbacks('upload', filename, -1, 0, filesize)
                        return False
                
                original_timeout = self.sock.gettimeout()
                # Dynamic timeout based on 1MB/s minimum speed + 60s
                upload_timeout = 60.0 + (filesize / (1024*1024)) 
                self.sock.settimeout(upload_timeout)
                
                self.sock.sendall(b'\x01') # TYPE 1: Upload
                self.sock.sendall(struct.pack('!I', len(meta_json)) + meta_json)
                
                total_sent = 0
                chunk_size_send = 65536
                last_progress_time = time.time()
                
                try:
                    with open(filepath, 'rb') as f:
                        while True:
                            chunk = f.read(chunk_size_send)
                            if not chunk:
                                break
                            
                            self.sock.sendall(chunk)
                            total_sent += len(chunk)
                            
                            current_time = time.time()
                            if filesize > 0 and (total_sent == filesize or 
                                                 current_time - last_progress_time > 0.2):
                                progress = (total_sent / filesize) * 100
                                self._notify_progress_callbacks('upload', filename, 
                                                                progress, total_sent, filesize)
                                last_progress_time = current_time
                            
                            if not self.running:
                                print("[FILE CLIENT WARN] Disconnected during upload")
                                return False
                                
                except IOError as e:
                    print(f"[FILE CLIENT ERROR] Error reading file: {e}")
                    # Let heartbeat loop handle disconnect
                    return False
                
                self.sock.settimeout(10.0) # Wait for server ACK
                response = self._recv_exact(1)
                self.sock.settimeout(original_timeout)
                original_timeout = None # Flag timeout reset
                
                if response == b'\x01': # Success ACK
                    print(f"[FILE CLIENT] Upload successful: {filename}")
                    self.files_uploaded += 1
                    self.bytes_uploaded += filesize
                    self._notify_progress_callbacks('upload', filename, 100, filesize, filesize)
                    self.last_heartbeat_time = time.time()
                    return True
                else:
                    print(f"[FILE CLIENT ERROR] Upload failed (response: {response})")
                    self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
                    return False
                    
        except socket.timeout:
            print(f"[FILE CLIENT ERROR] Upload timed out for {filename}")
            self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
            # Let heartbeat loop handle disconnect
            return False
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Upload connection error: {e}")
            self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
            # Let heartbeat loop handle disconnect
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected upload error: {e}")
            traceback.print_exc()
            self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
            # Let heartbeat loop handle disconnect
            return False
        finally:
            if self.sock and original_timeout is not None:
                try: self.sock.settimeout(original_timeout)
                except: pass

    def download_file(self, file_id, save_path=None):
        """
        Downloads a file from the server.
        [FIX] This function NO LONGER calls self.disconnect() on failure.
        """
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT ERROR] Cannot download: Not connected")
                return False
        
        file_info = None
        filename = f"file_{file_id}"
        filesize = 0
        actual_size = 0
        total_received = 0
        original_timeout = None
        
        try:
            with self.file_lock:
                file_info = next((f for f in self.available_files 
                                  if f.get('file_id') == file_id), None)
            
            if not file_info:
                print(f"[FILE CLIENT ERROR] File ID not found: {file_id}")
                self._notify_progress_callbacks('download', filename, -1, 0, 0)
                return False
            
            filename = file_info.get('filename', filename)
            filesize = file_info.get('filesize', 0)
            
            print(f"[FILE CLIENT] Requesting download for '{filename}' (ID: {file_id})...")
            self._notify_progress_callbacks('download', filename, 0, 0, filesize)
            
            with self.socket_lock:
                with self.connection_lock:
                    if not self.connected or not self.sock:
                        print("[FILE CLIENT ERROR] Cannot download: Not connected")
                        return False
                
                original_timeout = self.sock.gettimeout()
                # Generous dynamic timeout based on 50KB/s minimum + 30s
                download_timeout = 30.0 + (filesize / 50000) if filesize > 0 else 30.0
                self.sock.settimeout(download_timeout)
                
                self.sock.sendall(b'\x02') # TYPE 2: Download
                file_id_bytes = file_id.encode('utf-8')
                self.sock.sendall(struct.pack('!H', len(file_id_bytes)) + file_id_bytes)
                
                size_data = self._recv_exact(4)
                if not size_data:
                    print("[FILE CLIENT ERROR] Server disconnected")
                    return False
                
                actual_size = struct.unpack('!I', size_data)[0]
                
                if actual_size == 0:
                    print(f"[FILE CLIENT ERROR] File not available: {file_id}")
                    self._notify_progress_callbacks('download', filename, -1, 0, filesize)
                    self.sock.settimeout(original_timeout)
                    original_timeout = None
                    return False
                
                print(f"[FILE CLIENT] Downloading '{filename}' ({actual_size} bytes)...")
                
                if save_path is None:
                    save_path = os.path.join(self.download_dir, filename)
                else:
                    try:
                        os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    except OSError as e:
                        print(f"[FILE CLIENT ERROR] Cannot create directory: {e}")
                        self._notify_progress_callbacks('download', filename, -1, 0, actual_size)
                        self.sock.settimeout(original_timeout)
                        original_timeout = None
                        return False
                
                chunk_size_recv = 65536
                last_progress_time = time.time()
                
                try:
                    with open(save_path, 'wb') as f:
                        while total_received < actual_size:
                            remaining = actual_size - total_received
                            chunk = self.sock.recv(min(chunk_size_recv, remaining))
                            
                            if not chunk:
                                raise ConnectionError("Connection lost during download")
                            
                            f.write(chunk)
                            total_received += len(chunk)
                            
                            current_time = time.time()
                            if current_time - last_progress_time > 0.2 or total_received == actual_size:
                                progress = (total_received / actual_size) * 100
                                self._notify_progress_callbacks('download', filename, 
                                                                progress, total_received, actual_size)
                                last_progress_time = current_time
                            
                            if not self.running:
                                raise ConnectionAbortedError("Download aborted")
                                
                except IOError as e:
                    print(f"[FILE CLIENT ERROR] Could not save file: {e}")
                    self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
                    raise
                except (ConnectionError, ConnectionAbortedError) as e:
                    print(f"[FILE CLIENT ERROR] {e}")
                    if os.path.exists(save_path):
                        try: os.remove(save_path)
                        except: pass
                    raise
                
                print(f"[FILE CLIENT] Download successful: '{filename}' -> {save_path}")
                self.files_downloaded += 1
                self.bytes_downloaded += total_received
                
                if total_received == actual_size:
                    self._notify_progress_callbacks('download', filename, 100, actual_size, actual_size)
                
                self.sock.settimeout(original_timeout)
                original_timeout = None
                self.last_heartbeat_time = time.time()
                return True
                
        except socket.timeout:
            print(f"[FILE CLIENT ERROR] Download timed out for {filename}")
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            # Let heartbeat loop handle disconnect
            return False
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Download connection error: {e}")
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            # Let heartbeat loop handle disconnect
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected download error: {e}")
            traceback.print_exc()
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            # Let heartbeat loop handle disconnect
            return False
        finally:
            if self.sock and original_timeout is not None:
                try: self.sock.settimeout(original_timeout)
                except: pass

    def refresh_file_list(self):
        """
        Requests an updated file list from the server.
        [FIX] This function NO LONGER calls self.disconnect() on failure.
        """
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT WARN] Cannot refresh file list: Not connected.")
                return False
        
        original_timeout = None
        
        try:
            with self.socket_lock:
                with self.connection_lock:
                    if not self.connected or not self.sock:
                        print("[FILE CLIENT WARN] Cannot refresh file list: Not connected.")
                        return False
                
                original_timeout = self.sock.gettimeout()
                self.sock.settimeout(10.0)
                
                print("[FILE CLIENT] Requesting file list refresh...")
                self.sock.sendall(b'\x03') # TYPE 3: List Files
                
                success = self._receive_file_list(expected=True)
                
                self.sock.settimeout(original_timeout)
                original_timeout = None
                
                if success:
                    print("[FILE CLIENT] File list refreshed successfully")
                    # Notify callbacks that list has changed
                    # (A bit of a hack, send a generic notification)
                    self._notify_new_file_callbacks(None) 
                else:
                    print("[FILE CLIENT] File list refresh failed")
                
                return success
                
        except (socket.timeout, ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Refresh failed (connection issue): {e}")
            # Let heartbeat loop handle disconnect
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected refresh error: {e}")
            traceback.print_exc()
            # Let heartbeat loop handle disconnect
            return False
        finally:
            if self.sock and original_timeout is not None:
                try: self.sock.settimeout(original_timeout)
                except: pass

    def get_available_files(self):
        """Get list of available files."""
        with self.file_lock:
            return list(self.available_files)

    def is_connected(self):
        """Check if client is currently connected."""
        with self.connection_lock:
            return self.connected

    def register_new_file_callback(self, callback):
        """Register callback for new file notifications."""
        if callback not in self.new_file_callbacks:
            self.new_file_callbacks.append(callback)

    def register_progress_callback(self, callback):
        """Register callback for progress updates."""
        if callback not in self.progress_callbacks:
            self.progress_callbacks.append(callback)

    def register_connection_status_callback(self, callback):
        """Register callback for connection status changes."""
        if callback not in self.connection_status_callbacks:
            self.connection_status_callbacks.append(callback)

    def _notify_new_file_callbacks(self, notification):
        """Notify all new file callbacks.
           'None' notification means "list was refreshed"
        """
        for callback in self.new_file_callbacks:
            try:
                callback(notification)
            except Exception as e:
                print(f"[FILE CLIENT] New file callback error: {e}")

    def _notify_progress_callbacks(self, operation, filename, progress, current, total):
        """Notify all progress callbacks."""
        for callback in self.progress_callbacks:
            try:
                callback(operation, filename, progress, current, total)
            except Exception as e:
                print(f"[FILE CLIENT] Progress callback error: {e}")

    def _notify_connection_status(self, connected):
        """Notify all connection status callbacks."""
        for callback in self.connection_status_callbacks:
            try:
                callback(connected)
            except Exception as e:
                print(f"[FILE CLIENT] Connection status callback error: {e}")

    def _recv_exact(self, length):
        """
        Receive exactly 'length' bytes or return None on failure.
        Rethrows socket.timeout.
        """
        if not self.sock:
            return None
        
        data = b''
        try:
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk:
                    return None # Connection closed
                data += chunk
            return data
        except (ConnectionError, OSError):
            return None # Connection error
        except socket.timeout:
            raise # Re-throw timeout

    def disconnect(self):
        """Disconnect from the server."""
        print("[FILE CLIENT] Disconnecting...")
        
        was_connected = False
        with self.connection_lock:
            if self.running or self.connected:
                was_connected = True
            self.running = False
            self.connected = False
            self.auto_reconnect = False # Stop auto-reconnect
        
        if not was_connected:
            print("[FILE CLIENT] Already disconnected.")
            return
        
        sock = self.sock
        self.sock = None
        
        if sock:
            try:
                # Shut down read/write to wake up blocking threads
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass # Socket already closed
            try:
                sock.close()
            except OSError:
                pass
        
        # Wait for threads to stop
        if self.listen_thread and self.listen_thread.is_alive():
            self.listen_thread.join(timeout=0.5)
        
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=0.5)
        
        self._notify_connection_status(False)
        print(f"[FILE CLIENT] Stats - Uploaded: {self.files_uploaded}, Downloaded: {self.files_downloaded}")
        print("[FILE CLIENT] Disconnected")