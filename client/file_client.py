"""
File Client - Uploads and downloads files via TCP
[REWRITE] Complete rewrite to use a single-threaded "demultiplexer"
          and message queues to prevent all race conditions.
[FIXED] Eliminates the "Unexpected heartbeat response" bug.
[FIXED] Robust, stable connection and auto-reconnect logic.
[PATCHED] Fixed queue.Full crash in _handle_file_list
[PATCHED] Fixed AttributeError for listener_paused
"""

import socket
import threading
import struct
import json
import time
import os
import hashlib
from queue import Queue, Empty, Full # Import Full
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
        self.auto_reconnect = False
        self.reconnect_delay = 5

        self.available_files = []
        self.file_lock = threading.Lock()
        self.connection_lock = threading.Lock()
        
        # --- Callbacks ---
        self.new_file_callbacks = []
        self.progress_callbacks = []
        self.connection_status_callbacks = []

        # --- Queues for synchronous replies ---
        self.sync_reply_queues = {
            'upload_ack': Queue(maxsize=1),
            'file_list': Queue(maxsize=1),
            'download_header': Queue(maxsize=1),
        }

        # [CRITICAL FIX - INIT] Initialize the listener_paused event
        self.listener_paused = threading.Event()

        # --- Threads ---
        self.listener_thread = None
        self.heartbeat_thread = None
        self.last_pong_time = 0
        self.heartbeat_interval = 15

        self.download_dir = "client_downloads"
        os.makedirs(self.download_dir, exist_ok=True)
        
        # --- Stats ---
        self.files_uploaded = 0
        self.files_downloaded = 0
        self.bytes_uploaded = 0
        self.bytes_downloaded = 0

    def _clear_sync_queues(self):
        """Clears all reply queues, e.g., on disconnect."""
        for q in self.sync_reply_queues.values():
            while not q.empty():
                try: q.get_nowait()
                except Empty: break

    def connect(self, auto_reconnect=False):
        """Connect to the file server."""
        with self.connection_lock:
            if self.connected:
                print("[FILE CLIENT] Already connected.")
                return True
            if self.running and not auto_reconnect:
                print("[FILE CLIENT] Connection attempt already in progress.")
                return False

            self.auto_reconnect = auto_reconnect
            self.running = True
            
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(10.0) 
                
                print(f"[FILE CLIENT] Connecting to {self.server_ip}:{self.server_port}...")
                self.sock.connect((self.server_ip, self.server_port))
                
                username_bytes = self.username.encode('utf-8')
                if len(username_bytes) > 255:
                    username_bytes = username_bytes[:255]
                
                handshake = struct.pack('!IB', self.client_id, len(username_bytes)) + username_bytes
                self.sock.sendall(handshake)
                
                self.sock.settimeout(5.0) 
                ack = self._recv_exact(6)
                
                if ack and len(ack) == 6:
                    ack_id = struct.unpack('!I', ack[:4])[0]
                    ack_status = ack[4:]
                    
                    if ack_id == self.client_id and ack_status == b'OK':
                        print("[FILE CLIENT] Handshake OK.")
                        self.sock.settimeout(self.heartbeat_interval + 10.0)
                        
                        self.listener_thread = threading.Thread(target=self._main_listener, daemon=True)
                        self.listener_thread.start()
                        
                        print("[FILE CLIENT] Waiting for initial file list...")
                        try:
                            # [CRITICAL FIX - QUEUE] Clear stale replies before waiting
                            try: self.sync_reply_queues['file_list'].get_nowait()
                            except Empty: pass
                            
                            success = self.sync_reply_queues['file_list'].get(self.heartbeat_interval + 10.0)
                            if not success:
                                raise Exception("Failed to receive initial file list.")
                        except Empty:
                            raise Exception("Timeout waiting for initial file list.")
                        
                        self.connected = True
                        
                        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
                        self.heartbeat_thread.start()
                        self.last_pong_time = time.time()
                        
                        print(f"[FILE CLIENT] Connected to {self.server_ip}:{self.server_port}")
                        self._notify_connection_status(True)
                        return True
                    else:
                        print(f"[FILE CLIENT ERROR] Invalid ACK: ID={ack_id}, Status={ack_status}")
                else:
                    print(f"[FILE CLIENT ERROR] Failed to receive valid ACK")
                
            except Exception as e:
                print(f"[FILE CLIENT ERROR] Connection failed: {e}")
                if self.running: traceback.print_exc()

            self.running = False
            self.connected = False
            if self.sock:
                try: self.sock.close()
                except: pass
            self.sock = None
            return False

    def _main_listener(self):
        """
        This is the *ONLY* thread that calls sock.recv().
        It acts as a demultiplexer for all incoming messages.
        """
        print("[FILE CLIENT] Main listener started")
        while self.running:
            try:
                # [CRITICAL FIX] Check if listener is paused for download
                if self.listener_paused.is_set():
                    time.sleep(0.1)
                    continue

                # Wait for the first byte (message type)
                msg_type_data = self._recv_exact(1)
                
                if not msg_type_data:
                    if self.running:
                        print("[FILE CLIENT INFO] Server disconnected.")
                    break # Server closed connection

                msg_type = msg_type_data[0]

                if msg_type == 1: # 0x01: Upload ACK
                    # --- [MODIFICATION] ---
                    # Read 4-byte length
                    len_data = self._recv_exact(4)
                    if not len_data: continue # Connection error
                    
                    json_len = struct.unpack('!I', len_data)[0]
                    
                    reply_data = None # This will be None for failure
                    if json_len > 0 and json_len < 1024 * 10: # 10KB limit for info
                        json_data = self._recv_exact(json_len)
                        if json_data:
                            try:
                                reply_data = json.loads(json_data.decode('utf-8'))
                            except Exception as e:
                                print(f"[FILE CLIENT ERROR] Failed to parse upload ACK JSON: {e}")
                    
                    # reply_data will be a dict on success, or None on failure (json_len 0 or parse error)
                    try: 
                        self.sync_reply_queues['upload_ack'].put_nowait(reply_data)
                    except Full: 
                        print("[FILE CLIENT WARN] upload_ack queue was full.")
                    # --- [END MODIFICATION] ---
                
                elif msg_type == 2: # 0x02: Download Header
                    size_data = self._recv_exact(4)
                    try: self.sync_reply_queues['download_header'].put_nowait(size_data)
                    except Full: print("[FILE CLIENT WARN] download_header queue was full.")
                
                elif msg_type == 3: # 0x03: File List
                    self._handle_file_list()
                
                elif msg_type == 4: # 0x04: New File Notification
                    self._handle_notification()

                elif msg_type == 6: # 0x06: PONG
                    self.last_pong_time = time.time()
                
                else:
                    print(f"[FILE CLIENT WARN] Unknown message type received: {hex(msg_type)}")

            except socket.timeout:
                continue 
            except (ConnectionError, OSError) as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Connection error in listener: {e}")
                break
            except Exception as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Unexpected error in listener: {e}")
                    traceback.print_exc()
                break
        
        print("[FILE CLIENT] Main listener stopped.")
        self._trigger_disconnect()

    def _handle_notification(self):
        """Helper for _main_listener to process 0x04"""
        try:
            len_data = self._recv_exact(4)
            if not len_data: return
            notif_len = struct.unpack('!I', len_data)[0]
            
            if 0 < notif_len < 1024 * 10:
                notif_data = self._recv_exact(notif_len)
                if not notif_data: return
                
                notification = json.loads(notif_data.decode('utf-8'))
                self._process_new_file_notification(notification)
            else:
                print(f"[FILE CLIENT WARN] Invalid notification length: {notif_len}")
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Failed to process notification: {e}")

    def _handle_file_list(self):
        """Helper for _main_listener to process 0x03"""
        reply_queue = self.sync_reply_queues['file_list']
        try:
            list_len_data = self._recv_exact(4)
            if not list_len_data:
                # [CRITICAL FIX - QUEUE] Safely put reply
                try: reply_queue.put_nowait(False)
                except Full: print("[FILE CLIENT WARN] file_list queue was full. Dropping stale reply.")
                return
            
            list_len = struct.unpack('!I', list_len_data)[0]
            if list_len > 10 * 1024 * 1024: # 10MB limit
                print(f"[FILE CLIENT ERROR] File list too large: {list_len}")
                # [CRITICAL FIX - QUEUE] Safely put reply
                try: reply_queue.put_nowait(False)
                except Full: print("[FILE CLIENT WARN] file_list queue was full. Dropping stale reply.")
                return

            list_data = None
            if list_len > 0:
                list_data = self._recv_exact(list_len)
                if not list_data:
                    # [CRITICAL FIX - QUEUE] Safely put reply
                    try: reply_queue.put_nowait(False)
                    except Full: print("[FILE CLIENT WARN] file_list queue was full. Dropping stale reply.")
                    return
            
            file_list = []
            if list_data:
                file_list = json.loads(list_data.decode('utf-8'))

            with self.file_lock:
                self.available_files = file_list
            
            print(f"[FILE CLIENT] Received file list: {len(file_list)} files.")
            # [CRITICAL FIX - QUEUE] Safely put reply
            try: reply_queue.put_nowait(True)
            except Full: print("[FILE CLIENT WARN] file_list queue was full. Dropping stale reply.")
            
            self._notify_new_file_callbacks(None) # None = "list refreshed"
            return
            
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Failed to parse file list: {e}")
        
        # [CRITICAL FIX - QUEUE] Safely put reply
        try: reply_queue.put_nowait(False)
        except Full: print("[FILE CLIENT WARN] file_list queue was full. Dropping stale reply.")


    def _process_new_file_notification(self, notification):
        """Process and store new file info, notify GUI."""
        if notification.get('type') == 'new_file':
            file_info = {
                'file_id': notification.get('file_id'),
                'filename': notification.get('filename'),
                'filesize': notification.get('filesize'),
                'uploader': notification.get('uploader'),
                'timestamp': notification.get('timestamp')
            }
            
            if not all(file_info.get(k) is not None for k in ['file_id', 'filename', 'filesize', 'uploader']):
                print(f"[FILE CLIENT WARN] Invalid notification: {notification}")
                return
            
            with self.file_lock:
                if not any(f['file_id'] == file_info['file_id'] for f in self.available_files):
                    self.available_files.append(file_info)
                    print(f"[FILE CLIENT] New file available: {file_info['filename']}")
                    self._notify_new_file_callbacks(notification.copy())
                else:
                    print(f"[FILE CLIENT DEBUG] Received notification for known file: {file_info['filename']}")

    def _heartbeat_loop(self):
        """Sends PINGs and checks for PONGs (received by listener)."""
        print("[FILE CLIENT] Heartbeat monitor started")
        while self.running:
            try:
                for _ in range(self.heartbeat_interval):
                    if not self.running: break
                    time.sleep(1)
                
                if not self.running: break
                
                time_since_last_pong = time.time() - self.last_pong_time
                
                if time_since_last_pong > (self.heartbeat_interval * 2 + 5):
                    print(f"[FILE CLIENT ERROR] No PONG received in {time_since_last_pong:.1f}s. Connection lost.")
                    self._trigger_disconnect()
                    break
                
                try:
                    if self.sock and self.running:
                        self.sock.sendall(b'\x05') # PING
                except Exception as e:
                    print(f"[FILE CLIENT WARN] Heartbeat PING send failed: {e}")
                        
            except Exception as e:
                if self.running:
                    print(f"[FILE CLIENT ERROR] Heartbeat loop error: {e}")
                time.sleep(1)
        
        print("[FILE CLIENT] Heartbeat monitor stopped")

    def _trigger_disconnect(self):
        """
        Internal function to handle connection loss.
        """
        with self.connection_lock:
            if not self.running: # Already disconnecting
                return
            
            print("[FILE CLIENT] Connection lost. Triggering disconnect.")
            self.running = False 
            self.connected = False
            self._notify_connection_status(False)
            
            if self.sock:
                try: self.sock.close()
                except: pass
                self.sock = None
            
            self._clear_sync_queues()
            
            auto_reconnect_flag = self.auto_reconnect 
        
        if auto_reconnect_flag:
            print("[FILE CLIENT] Attempting auto-reconnect...")
            threading.Thread(target=self._attempt_reconnect, daemon=True).start()
        else:
            self.disconnect() 

    def _attempt_reconnect(self):
        """Internal thread to loop connection attempts."""
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            with self.connection_lock:
                if not self.auto_reconnect: 
                    print("[FILE CLIENT] Auto-reconnect cancelled.")
                    return
            
            print(f"[FILE CLIENT] Reconnection attempt {attempt}/{max_attempts}...")
            
            if self.connect(auto_reconnect=True):
                print("[FILE CLIENT] Reconnection successful!")
                return
            
            for _ in range(self.reconnect_delay):
                with self.connection_lock:
                    if not self.auto_reconnect: break
                time.sleep(1)

        print("[FILE CLIENT] Reconnection failed after all attempts")
        self.disconnect() 


    def upload_file(self, filepath, recipient_id=None):
        """Uploads a file to the server. If recipient_id is provided, sends as private file."""
        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            print(f"[FILE CLIENT ERROR] File not found: {filepath}")
            self._notify_progress_callbacks('upload', os.path.basename(filepath), -1, 0, 0)
            return None # Return None on failure
        
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT ERROR] Cannot upload: Not connected")
                self._notify_progress_callbacks('upload', os.path.basename(filepath), -1, 0, 0)
                return None # Return None on failure
        
        filename = os.path.basename(filepath)
        filesize = 0
        total_sent = 0
        
        try:
            filesize = os.path.getsize(filepath)
            
            if filesize > 1 * 1024 * 1024 * 1024: # 1GB limit
                print(f"[FILE CLIENT ERROR] File too large: {filesize} bytes")
                self._notify_progress_callbacks('upload', filename, -1, 0, filesize)
                return None # Return None on failure
            
            print(f"[FILE CLIENT] Starting upload for '{filename}' ({filesize} bytes)...")
            self._notify_progress_callbacks('upload', filename, 0, 0, filesize)
            
            file_hash = hashlib.sha256()
            try:
                with open(filepath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        file_hash.update(chunk)
                file_hash_hex = file_hash.hexdigest()
            except IOError as e:
                print(f"[FILE CLIENT ERROR] Could not read file: {e}")
                self._notify_progress_callbacks('upload', filename, -1, 0, filesize)
                return None # Return None on failure
            
            metadata = {'filename': filename, 'filesize': filesize, 'hash': file_hash_hex}
            # Add recipient_id if this is a private file
            if recipient_id is not None:
                metadata['recipient_id'] = recipient_id
                metadata['is_private'] = True
            meta_json = json.dumps(metadata).encode('utf-8')
            
            # [CRITICAL FIX - QUEUE] Clear stale ACK before sending
            try: self.sync_reply_queues['upload_ack'].get_nowait()
            except Empty: pass

            self.sock.sendall(b'\x01') # TYPE 1: Upload
            self.sock.sendall(struct.pack('!I', len(meta_json)) + meta_json)
            
            total_sent = 0
            chunk_size_send = 65536
            last_progress_time = time.time()
            
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size_send)
                    if not chunk: break
                    self.sock.sendall(chunk)
                    total_sent += len(chunk)
                    
                    current_time = time.time()
                    if filesize > 0 and (total_sent == filesize or 
                                         current_time - last_progress_time > 0.2):
                        progress = (total_sent / filesize) * 100
                        self._notify_progress_callbacks('upload', filename, 
                                                        progress, total_sent, filesize)
                        last_progress_time = current_time
            
            ack_timeout = 60.0 + (filesize / (1024*1024)) 
            try:
                # --- [MODIFICATION] ---
                # Expect a dict (file_info) on success, None on failure
                response_data = self.sync_reply_queues['upload_ack'].get(timeout=ack_timeout)
                
                if isinstance(response_data, dict): # Success ACK
                    print(f"[FILE CLIENT] Upload successful: {filename}")
                    self.files_uploaded += 1
                    self.bytes_uploaded += filesize
                    
                    # --- ADD TO LOCAL LIST ---
                    # Add the file to our own list immediately
                    with self.file_lock:
                        if not any(f['file_id'] == response_data['file_id'] for f in self.available_files):
                            self.available_files.append(response_data)
                            
                            # [CRITICAL] Create the notification object the GUI expects
                            gui_notification = {
                                'type': 'new_file',
                                'file_id': response_data.get('file_id'),
                                'filename': response_data.get('filename'),
                                'filesize': response_data.get('filesize'),
                                'uploader': response_data.get('uploader'),
                                'timestamp': response_data.get('timestamp')
                            }
                            self._notify_new_file_callbacks(gui_notification)
                    # --- END ADD ---
                    
                    self._notify_progress_callbacks('upload', filename, 100, filesize, filesize)
                    return response_data # Return the file_info dict
                else:
                    print(f"[FILE CLIENT ERROR] Upload failed (server rejected file)")
                    self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
                    return None # Return None on failure
                # --- [END MODIFICATION] ---
            except Empty:
                print("[FILE CLIENT ERROR] Timeout waiting for upload ACK from server.")
                self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
                self._trigger_disconnect()
                return None # Return None on failure
                    
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Upload connection error: {e}")
            self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
            self._trigger_disconnect()
            return None # Return None on failure
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected upload error: {e}")
            traceback.print_exc()
            self._notify_progress_callbacks('upload', filename, -1, total_sent, filesize)
            self._trigger_disconnect()
            return None # Return None on failure

    def download_file(self, file_id, save_path=None):
        """Downloads a file from the server."""
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT ERROR] Cannot download: Not connected")
                return False
        
        file_info = None
        filename = f"file_{file_id}"
        filesize = 0
        total_received = 0
        
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
            
            try: self.sync_reply_queues['download_header'].get_nowait()
            except Empty: pass
            self.sock.sendall(b'\x02') 
            file_id_bytes = file_id.encode('utf-8')
            self.sock.sendall(struct.pack('!H', len(file_id_bytes)) + file_id_bytes)
            
            try:
                size_data = self.sync_reply_queues['download_header'].get(timeout=15.0)
            except Empty:
                print("[FILE CLIENT ERROR] Timeout waiting for download header from server.")
                self._trigger_disconnect()
                return False
                
            actual_size = struct.unpack('!I', size_data)[0]
            
            if actual_size == 0:
                print(f"[FILE CLIENT ERROR] File not available on server: {file_id}")
                self._notify_progress_callbacks('download', filename, -1, 0, filesize)
                return False
            
            print(f"[FILE CLIENT] Downloading '{filename}' ({actual_size} bytes)...")
            
            if save_path is None:
                save_path = os.path.join(self.download_dir, filename)
            else:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # --- Tell listener to pause ---
            self.listener_paused.set() 
            
            download_timeout = 30.0 + (actual_size / 50000)
            self.sock.settimeout(download_timeout) 

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
                    
                print(f"[FILE CLIENT] Download successful: '{filename}' -> {save_path}")
                self.files_downloaded += 1
                self.bytes_downloaded += total_received
                self._notify_progress_callbacks('download', filename, 100, actual_size, actual_size)
                return True

            except (IOError, ConnectionError, ConnectionAbortedError) as e:
                print(f"[FILE CLIENT ERROR] Download failed: {e}")
                if os.path.exists(save_path):
                    try: os.remove(save_path)
                    except: pass
                self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
                self._trigger_disconnect() 
                return False
            
        except socket.timeout:
            print(f"[FILE CLIENT ERROR] Download timed out for {filename}")
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            self._trigger_disconnect()
            return False
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Download connection error: {e}")
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            self._trigger_disconnect()
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected download error: {e}")
            traceback.print_exc()
            self._notify_progress_callbacks('download', filename, -1, total_received, actual_size)
            self._trigger_disconnect()
            return False
        finally:
            # --- CRITICAL: Resume listener thread ---
            self.listener_paused.clear()
            if self.sock:
                try: self.sock.settimeout(self.heartbeat_interval + 10.0)
                except: pass

    def refresh_file_list(self):
        """Requests an updated file list from the server."""
        with self.connection_lock:
            if not self.connected:
                print("[FILE CLIENT WARN] Cannot refresh file list: Not connected.")
                return False
        
        try:
            print("[FILE CLIENT] Requesting file list refresh...")
            
            # [CRITICAL FIX - QUEUE] Clear stale reply
            try:
                self.sync_reply_queues['file_list'].get_nowait()
                print("[FILE CLIENT DEBUG] Cleared stale file list reply.")
            except Empty:
                pass 
            
            self.sock.sendall(b'\x03') # TYPE 3: List Files
            
            try:
                success = self.sync_reply_queues['file_list'].get(timeout=10.0)
                if success:
                    print("[FILE CLIENT] File list refreshed successfully")
                    return True
                else:
                    print("[FILE CLIENT] File list refresh failed (server error)")
                    return False
            except Empty:
                print("[FILE CLIENT ERROR] Timeout waiting for file list refresh")
                self._trigger_disconnect()
                return False
                
        except (ConnectionError, OSError) as e:
            print(f"[FILE CLIENT ERROR] Refresh failed (connection issue): {e}")
            self._trigger_disconnect()
            return False
        except Exception as e:
            print(f"[FILE CLIENT ERROR] Unexpected refresh error: {e}")
            self._trigger_disconnect()
            return False

    def get_available_files(self):
        with self.file_lock:
            return list(self.available_files)

    def is_connected(self):
        with self.connection_lock:
            return self.connected

    def register_new_file_callback(self, callback):
        if callback not in self.new_file_callbacks:
            self.new_file_callbacks.append(callback)

    def register_progress_callback(self, callback):
        if callback not in self.progress_callbacks:
            self.progress_callbacks.append(callback)

    def register_connection_status_callback(self, callback):
        if callback not in self.connection_status_callbacks:
            self.connection_status_callbacks.append(callback)

    def _notify_new_file_callbacks(self, notification=None):
        for callback in self.new_file_callbacks:
            try: callback(notification)
            except Exception as e: print(f"[FILE CLIENT] New file callback error: {e}")

    def _notify_progress_callbacks(self, op, filename, progress, current, total):
        for callback in self.progress_callbacks:
            try: callback(op, filename, progress, current, total)
            except Exception as e: print(f"[FILE CLIENT] Progress callback error: {e}")

    def _notify_connection_status(self, connected):
        for callback in self.connection_status_callbacks:
            try: callback(connected)
            except Exception as e: print(f"[FILE CLIENT] Connection status callback error: {e}")

    def _recv_exact(self, length):
        """
        Receive exactly 'length' bytes or return None on failure.
        """
        if not self.sock:
            return None
        
        data = b''
        try:
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk:
                    return None 
                data += chunk
            return data
        except (ConnectionError, OSError):
            return None
        except socket.timeout:
            raise 

    def disconnect(self):
        """Gracefully disconnect from the server."""
        print("[FILE CLIENT] Disconnecting...")
        
        with self.connection_lock:
            if not self.running:
                print("[FILE CLIENT] Already disconnected.")
                return
            
            self.running = False
            self.connected = False
            self.auto_reconnect = False
        
        self.listener_paused.set() # Stop listener loop
        self._clear_sync_queues()

        sock = self.sock
        self.sock = None
        
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=1.0)
        
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=1.0)
        
        self._notify_connection_status(False)
        print(f"[FILE CLIENT] Stats - Uploaded: {self.files_uploaded}, Downloaded: {self.files_downloaded}")
        print("[FILE CLIENT] Disconnected")