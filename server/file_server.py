"""
File Server - Manages file transfers between clients using TCP.
[FIXED] Added PING/PONG heartbeat response
[FIXED] Ensures file list is sent only explicitly or after upload notification.
[FIX] Broadcast notification now sends to *all* clients, including uploader.
[CLEANUP] Removed redundant OS-level TCP Keepalives
"""
import socket
import threading
import struct
import json
import time
import os
import hashlib
from datetime import datetime
import sys
import traceback # For logging
# Use relative path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.config import *

class FileServer:
    def __init__(self, host=SERVER_HOST, port=FILE_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.clients = {} # {client_id: {'socket': sock, 'address': addr, 'username': name}}
        self.client_lock = threading.Lock()
        self.files = {} # {file_id: {metadata}}
        self.file_lock = threading.Lock()
        self.storage_dir = "server_files"
        os.makedirs(self.storage_dir, exist_ok=True)
        self.total_files_ever = 0
        self.total_bytes_stored = 0

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.listen(MAX_CLIENTS)
            self.running = True
            print(f"[FILE SERVER] Started on {self.host}:{self.port}")
            print(f"[FILE SERVER] Storage directory: {os.path.abspath(self.storage_dir)}")
            accept_thread = threading.Thread(target=self._accept_connections, daemon=True)
            accept_thread.start()
            return True
        except Exception as e:
            print(f"[FILE SERVER ERROR] Failed to start: {e}")
            self.running = False
            return False

    def _accept_connections(self):
        while self.running:
            try:
                client_sock, client_addr = self.sock.accept()
                # Removed redundant SO_KEEPALIVE options
                handler_thread = threading.Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True)
                handler_thread.start()
            except OSError:
                if self.running: print("[FILE SERVER] Socket closed, stopping accept loop.")
                break
            except Exception as e:
                if self.running: print(f"[FILE SERVER ERROR] Accept error: {e}")


    def _handle_client(self, client_sock, client_addr):
        client_id = None
        username = "Unknown"
        client_added = False
        try:
            client_sock.settimeout(10.0) # Handshake timeout
            handshake_header = self._recv_exact(client_sock, 5)
            if not handshake_header: return

            client_id = struct.unpack('!I', handshake_header[:4])[0]
            username_len = handshake_header[4]
            if username_len > 0:
                username_bytes = self._recv_exact(client_sock, username_len)
                if not username_bytes: return
                username = username_bytes.decode('utf-8', errors='ignore')

            with self.client_lock:
                if client_id in self.clients:
                    print(f"[FILE SERVER WARN] Client {client_id} already connected. Rejecting new connection.")
                    try: client_sock.close()
                    except: pass
                    return
                self.clients[client_id] = {'socket': client_sock, 'address': client_addr, 'username': username}
                client_added = True
            print(f"[FILE SERVER] Client {client_id} ({username}) connected from {client_addr}")

            if not self._send_ack(client_sock, client_id): return
            if not self._send_file_list(client_sock): return

            client_sock.settimeout(CONNECTION_TIMEOUT + 15.0)

            while self.running:
                with self.client_lock:
                    is_current_socket = (client_id in self.clients and self.clients[client_id]['socket'] == client_sock)
                if not is_current_socket:
                    print(f"[FILE SERVER DEBUG] Socket mismatch for {client_id}, exiting handler.")
                    break

                req_type_data = self._recv_exact(client_sock, 1)
                if not req_type_data:
                    print(f"[FILE SERVER INFO] Client {client_id} disconnected gracefully.")
                    break

                req_type = req_type_data[0]

                if req_type == 1: # Upload
                    self._handle_upload(client_sock, client_id, username)
                elif req_type == 2: # Download
                    self._handle_download(client_sock, client_id, username)
                elif req_type == 3: # List Files (Refresh request)
                    client_sock.settimeout(15.0)
                    self._send_file_list(client_sock)
                    client_sock.settimeout(CONNECTION_TIMEOUT + 15.0) # Restore
                
                # --- THIS IS THE CRITICAL FIX ---
                elif req_type == 5: # PING
                    try:
                        client_sock.sendall(b'\x06') # PONG
                    except Exception as e:
                        print(f"[FILE SERVER WARN] Failed to send PONG to {client_id}: {e}")
                        break # Connection is likely broken
                # --- END FIX ---
                
                else:
                    print(f"[FILE SERVER WARN] Client {client_id} sent unknown request type: {req_type}")
                    time.sleep(0.1) # Prevent spamming

        except (socket.timeout, ConnectionError, OSError) as e:
            if self.running and client_added:
                print(f"[FILE SERVER INFO] Connection issue/timeout for {client_id if client_id else client_addr}: {e}")
        except Exception as e:
            if self.running and client_added:
                print(f"[FILE SERVER ERROR] Unexpected error handling {client_id if client_id else client_addr}: {e}")
                traceback.print_exc()
        finally:
            if client_id:
                removed = False
                with self.client_lock:
                    if client_id in self.clients and self.clients[client_id]['socket'] == client_sock:
                        del self.clients[client_id]
                        removed = True
                if removed:
                    print(f"[FILE SERVER] Client {client_id} ({username}) disconnected.")
            else:
                print(f"[FILE SERVER] Cleaning up failed connection from {client_addr}")

            try: client_sock.close()
            except: pass


    def _send_ack(self, client_sock, client_id):
        try:
            ack_msg = struct.pack('!I', client_id) + b'OK'
            client_sock.sendall(ack_msg)
            return True
        except Exception as e:
            print(f"[FILE SERVER WARN] Failed to send ACK to {client_id}: {e}")
            return False

    def _handle_upload(self, client_sock, client_id, username):
        file_id = None
        file_path = None
        filesize = 0
        original_timeout = client_sock.gettimeout()
        try:
            client_sock.settimeout(15.0)
            meta_len_data = self._recv_exact(client_sock, 4)
            if not meta_len_data: raise ConnectionAbortedError("Client disconnected before sending metadata length.")
            meta_len = struct.unpack('!I', meta_len_data)[0]

            if meta_len == 0 or meta_len > 4096:
                print(f"[FILE SERVER WARN] Client {client_id} sent invalid metadata length: {meta_len}")
                client_sock.sendall(b'\x00') # Send failure
                return

            meta_data = self._recv_exact(client_sock, meta_len)
            if not meta_data: raise ConnectionAbortedError("Client disconnected before sending metadata.")
            metadata = json.loads(meta_data.decode('utf-8'))

            filename = metadata.get('filename')
            filesize = metadata.get('filesize')
            client_hash = metadata.get('hash')

            if not filename or not isinstance(filesize, int) or filesize < 0:
                print(f"[FILE SERVER WARN] Client {client_id} sent invalid metadata content: {metadata}")
                client_sock.sendall(b'\x00')
                return

            upload_timeout = 60.0 + (filesize / 50000) if filesize > 0 else 60.0
            client_sock.settimeout(upload_timeout)

            print(f"[FILE SERVER] Receiving '{filename}' ({filesize} bytes) from {username} ({client_id})")

            unique_part = hashlib.sha1(f"{time.time()}_{client_id}_{filename}".encode()).hexdigest()[:10]
            file_id = f"{int(time.time())}_{unique_part}"
            file_path = os.path.join(self.storage_dir, file_id)

            server_hash = hashlib.sha256()
            bytes_received = 0
            chunk_size_recv = 65536

            try:
                with open(file_path, 'wb') as f:
                    while bytes_received < filesize:
                        remaining = filesize - bytes_received
                        chunk = client_sock.recv(min(chunk_size_recv, remaining))
                        if not chunk:
                            raise ConnectionError("Client disconnected during upload data transfer.")
                        f.write(chunk)
                        server_hash.update(chunk)
                        bytes_received += len(chunk)
            except IOError as e:
                print(f"[FILE SERVER ERROR] Cannot write file {file_path}: {e}")
                client_sock.sendall(b'\x00')
                if os.path.exists(file_path): os.remove(file_path)
                return

            server_hash_hex = server_hash.hexdigest()

            if client_hash and client_hash != server_hash_hex:
                print(f"[FILE SERVER WARN] Hash mismatch for '{filename}' from {client_id}. Client: {client_hash[:8]}..., Server: {server_hash_hex[:8]}.... Keeping file.")

            file_info_to_store = {
                'file_id': file_id, 'filename': filename, 'filesize': filesize,
                'hash': server_hash_hex, 'uploader': username, 'uploader_id': client_id,
                'timestamp': datetime.now().isoformat(), 'path': file_path
            }
            with self.file_lock:
                self.files[file_id] = file_info_to_store
                self.total_files_ever += 1
                self.total_bytes_stored += filesize

            print(f"[FILE SERVER] File '{filename}' saved as {file_id}")
            client_sock.settimeout(5.0)
            client_sock.sendall(b'\x01') # Send success

            # --- Broadcast notification AFTER sending success ---
            threading.Thread(target=self._broadcast_file_notification,
                             # --- FIX: Pass None to notify ALL clients (including uploader) ---
                             args=(file_info_to_store.copy(), None),
                             daemon=True).start()

        except (socket.timeout, ConnectionError, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[FILE SERVER ERROR] Error during upload from {client_id}: {e}")
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except OSError: pass
            try:
                if client_sock and client_sock.fileno() != -1:
                        client_sock.settimeout(2.0)
                        client_sock.sendall(b'\x00')
            except: pass
        except Exception as e:
            print(f"[FILE SERVER ERROR] Unexpected upload error from {client_id}: {e}")
            traceback.print_exc()
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except OSError: pass
            try:
                if client_sock and client_sock.fileno() != -1:
                    client_sock.settimeout(2.0)
                    client_sock.sendall(b'\x00')
            except: pass
        finally:
             try:
                if client_sock and client_sock.fileno() != -1:
                        client_sock.settimeout(original_timeout)
             except: pass


    def _handle_download(self, client_sock, client_id, username):
        file_path = None
        filename = "unknown_file"
        original_timeout = client_sock.gettimeout()
        try:
            client_sock.settimeout(10.0)
            id_len_data = self._recv_exact(client_sock, 2)
            if not id_len_data: raise ConnectionAbortedError("Client disconnected before sending file ID length.")
            id_len = struct.unpack('!H', id_len_data)[0]

            if id_len == 0 or id_len > 256:
                print(f"[FILE SERVER WARN] Client {client_id} sent invalid file ID length: {id_len}")
                client_sock.sendall(struct.pack('!I', 0))
                return

            file_id = self._recv_exact(client_sock, id_len).decode('utf-8')

            with self.file_lock:
                file_info = self.files.get(file_id)

            file_path_from_info = file_info.get('path', '') if file_info else ''
            if not file_info or not file_path_from_info or not os.path.exists(file_path_from_info):
                print(f"[FILE SERVER INFO] File not found or path invalid for ID: {file_id}, requested by {client_id}")
                client_sock.sendall(struct.pack('!I', 0))
                return

            file_path = file_path_from_info
            filesize = file_info['filesize']
            filename = file_info['filename']

            print(f"[FILE SERVER] Sending '{filename}' ({filesize} bytes) to {username} ({client_id})")

            download_timeout = 60.0 + (filesize / 50000) if filesize > 0 else 60.0
            client_sock.settimeout(download_timeout)

            client_sock.sendall(struct.pack('!I', filesize))

            chunk_size_send = 65536
            bytes_sent = 0
            try:
                with open(file_path, 'rb') as f:
                    while True:
                        chunk = f.read(chunk_size_send)
                        if not chunk: break
                        client_sock.sendall(chunk)
                        bytes_sent += len(chunk)
            except IOError as e:
                print(f"[FILE SERVER ERROR] Cannot read file {file_path} for download: {e}")
                return

            print(f"[FILE SERVER] Sent {bytes_sent} bytes for '{filename}' to {client_id}")

        except (socket.timeout, ConnectionError, OSError) as e:
            print(f"[FILE SERVER ERROR] Connection error during download for {client_id} ('{filename}'): {e}")
        except Exception as e:
            print(f"[FILE SERVER ERROR] Unexpected download error for {client_id} ('{filename}'): {e}")
            traceback.print_exc()
        finally:
             try:
                if client_sock and client_sock.fileno() != -1:
                        client_sock.settimeout(original_timeout)
             except: pass


    def _send_file_list(self, client_sock):
        try:
            with self.file_lock:
                file_list_serializable = [
                    {k: v for k, v in info.items() if k != 'path'}
                    for info in self.files.values()
                ]
            list_json = json.dumps(file_list_serializable).encode('utf-8')
            client_sock.sendall(struct.pack('!I', len(list_json)) + list_json)
            return True
        except Exception as e:
            print(f"[FILE SERVER ERROR] Failed to send file list: {e}")
            return False

    def _broadcast_file_notification(self, file_info, exclude_client_id=None):
        notification = {
            'type': 'new_file',
            'file_id': file_info['file_id'], 'filename': file_info['filename'],
            'filesize': file_info['filesize'], 'uploader': file_info['uploader'],
            'timestamp': file_info['timestamp']
        }
        try:
            notif_json = json.dumps(notification).encode('utf-8')
            notif_packet = b'\x04' + struct.pack('!I', len(notif_json)) + notif_json
        except Exception as e:
            print(f"[FILE SERVER ERROR] Failed to create notification packet: {e}")
            return

        disconnected_clients = []
        with self.client_lock:
            current_clients = list(self.clients.items())

        for cid, info in current_clients:
            if cid == exclude_client_id: continue
            try:
                info['socket'].sendall(notif_packet)
            except (OSError, ConnectionError):
                 print(f"[FILE SERVER DEBUG] Failed notification send to {cid} (likely disconnected).")
                 disconnected_clients.append(cid)
            except Exception as e:
                 print(f"[FILE SERVER WARN] Unexpected error sending notification to {cid}: {e}")
                 disconnected_clients.append(cid)

        if disconnected_clients:
            with self.client_lock:
                for cid in disconnected_clients:
                    if cid in self.clients:
                        print(f"[FILE SERVER INFO] Client {cid} marked disconnected during notification.")
                        try: self.clients[cid]['socket'].close()
                        except: pass
                        del self.clients[cid]


    def _recv_exact(self, sock, length):
        data = b''
        try:
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk: return None
                data += chunk
            return data
        except (socket.timeout):
            raise # Re-throw timeout
        except (ConnectionError, OSError):
            return None

    def get_stats(self):
        with self.client_lock, self.file_lock:
             return {
                 'active_clients': len(self.clients),
                 'total_files_ever': self.total_files_ever,
                 'available_files': len(self.files),
                 'total_bytes_stored': self.total_bytes_stored
             }

    def stop(self):
        print("[FILE SERVER] Stopping...")
        if not self.running: return
        self.running = False
        if self.sock:
            try: self.sock.close(); self.sock = None
            except: pass
        with self.client_lock:
            clients_copy = list(self.clients.values())
            self.clients.clear()
        for info in clients_copy:
            try: info['socket'].close()
            except: pass
        print("[FILE SERVER] Stopped")