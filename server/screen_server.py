# server/screen_server.py
"""
Screen Share Server - Manages a single presenter and broadcasts their
stream and status updates to all other connected clients via TCP.
"""
import socket
import threading
import struct
import time
import sys
import os
from collections import defaultdict

# Add project root to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.config import *

class ScreenServer:
    def __init__(self, host=SERVER_HOST, port=SCREEN_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        
        # {client_id: socket}
        self.clients = {}
        self.client_lock = threading.Lock()
        
        # The ID of the client currently presenting
        self.presenter_id = None
        self.presenter_lock = threading.Lock()
        
        self.stats = defaultdict(lambda: {'bytes_sent': 0, 'bytes_recv': 0})

    def start(self):
        """Starts the server and accept loop."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.listen(MAX_CLIENTS)
            self.running = True
            print(f"[SCREEN SERVER] Started on {self.host}:{self.port}")
            
            accept_thread = threading.Thread(target=self._accept_connections, daemon=True)
            accept_thread.start()
            return True
        except Exception as e:
            print(f"[SCREEN SERVER ERROR] Failed to start: {e}")
            return False

    def _accept_connections(self):
        """Main loop to accept new client connections."""
        while self.running:
            try:
                client_sock, client_addr = self.sock.accept()
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
                
                handler_thread = threading.Thread(target=self._handle_client, 
                                                args=(client_sock, client_addr), 
                                                daemon=True)
                handler_thread.start()
            except OSError:
                if self.running: print("[SCREEN SERVER] Socket closed, stopping accept loop.")
                break
            except Exception as e:
                if self.running: print(f"[SCREEN SERVER ERROR] Accept error: {e}")

    def _handle_client(self, client_sock, client_addr):
        """Handles a single client connection, handshake, and message loop."""
        client_id = None
        try:
            # 1. --- HANDSHAKE: Receive Client ID ---
            client_sock.settimeout(10.0) # Handshake timeout
            id_data = self._recv_exact(client_sock, 4)
            if not id_data:
                print(f"[SCREEN SERVER] Handshake failed: No data from {client_addr}")
                client_sock.close()
                return
                
            client_id = struct.unpack('!I', id_data)[0]

            # 2. --- HANDSHAKE: Check for existing ID & Send ACK ---
            with self.client_lock:
                if client_id in self.clients:
                    # Reject connection if ID is already active
                    print(f"[SCREEN SERVER WARN] Client {client_id} already connected. Rejecting.")
                    client_sock.sendall(b'\x00') # Send REJECT byte
                    client_sock.close()
                    return
                
                # Add new client to the list
                self.clients[client_id] = client_sock
                
            # Send ACK byte (b'\x01') to signal success
            client_sock.sendall(b'\x01')
            client_sock.settimeout(None) # Set to blocking for main loop
            
            print(f"[SCREEN SERVER] Client {client_id} connected from {client_addr}")

            # Send the new client the current presenter status
            self._send_status_update(client_sock)
            
            # --- MAIN MESSAGE LOOP ---
            while self.running:
                # Read command header (1 byte)
                cmd_data = self._recv_exact(client_sock, 1)
                if not cmd_data:
                    break # Client disconnected
                
                cmd = cmd_data[0]
                
                if cmd == 1: # START command
                    self._set_presenter(client_id)
                
                elif cmd == 2: # STOP command
                    if self.presenter_id == client_id:
                        self._set_presenter(None) # Clear presenter
                
                elif cmd == 3: # FRAME command
                    if self.presenter_id != client_id:
                        continue # Ignore frames from non-presenters
                    
                    # Read frame length (4 bytes)
                    len_data = self._recv_exact(client_sock, 4)
                    if not len_data: break
                    frame_len = struct.unpack('!I', len_data)[0]
                    
                    # Read frame data
                    frame_data = self._recv_exact(client_sock, frame_len)
                    if not frame_data: break
                    
                    self.stats[client_id]['bytes_recv'] += (frame_len + 5)
                    # Broadcast the frame to all *other* clients
                    self._broadcast_frame(frame_data, client_id)

        except (socket.timeout, ConnectionError, OSError) as e:
            print(f"[SCREEN SERVER INFO] Connection issue with {client_id}: {e}")
        except Exception as e:
            print(f"[SCREEN SERVER ERROR] Unexpected error handling {client_id}: {e}")
        finally:
            if client_id:
                print(f"[SCREEN SERVER] Client {client_id} disconnected.")
                with self.client_lock:
                    self.clients.pop(client_id, None)
                
                # If the disconnected client was the presenter, stop the presentation
                if self.presenter_id == client_id:
                    self._set_presenter(None)
            
            try: client_sock.close()
            except: pass

    def _set_presenter(self, new_presenter_id):
        """Sets the current presenter and notifies all clients."""
        with self.presenter_lock:
            if self.presenter_id == new_presenter_id:
                return # No change
            
            self.presenter_id = new_presenter_id
            
            if new_presenter_id:
                print(f"[SCREEN SERVER] Client {new_presenter_id} is now presenting.")
            else:
                print("[SCREEN SERVER] Screen sharing has stopped.")
        
        # Broadcast the new status to everyone
        self._broadcast_status_update()

    def _send_status_update(self, sock):
        """Sends a single status update packet to one socket."""
        try:
            # Packet: [1:cmd_code=10 'STATUS'] [4:presenter_id]
            presenter_id_int = self.presenter_id if self.presenter_id else 0
            packet = struct.pack('!BI', 10, presenter_id_int)
            sock.sendall(packet)
        except (OSError, ConnectionError):
            pass # Will be cleaned up by the caller's loop

    def _broadcast_status_update(self):
        """Broadcasts the current presenter status to all connected clients."""
        with self.client_lock:
            # Iterate over a copy in case the dict changes
            for sock in list(self.clients.values()):
                self._send_status_update(sock)

    def _broadcast_frame(self, frame_data, sender_id):
        """Broadcasts a frame packet to all clients except the sender."""
        # Packet: [1:cmd_code=11 'FRAME'] [4:data_len] [data]
        packet = struct.pack('!BI', 11, len(frame_data)) + frame_data
        
        with self.client_lock:
            # Iterate over a copy in case the dict changes
            for cid, sock in list(self.clients.items()):
                if cid != sender_id:
                    try:
                        sock.sendall(packet)
                        self.stats[cid]['bytes_sent'] += len(packet)
                    except (OSError, ConnectionError):
                        pass # Will be cleaned up by the client's handler

    def _recv_exact(self, sock, length):
        """Receive exactly 'length' bytes or return None on failure."""
        data = b''
        try:
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk:
                    return None
                data += chunk
            return data
        except (socket.timeout, ConnectionError, OSError):
            return None

    def stop(self):
        """Stops the screen server."""
        print("[SCREEN SERVER] Stopping...")
        self.running = False
        
        if self.sock:
            try: self.sock.close(); self.sock = None
            except: pass
        
        with self.client_lock:
            clients_copy = list(self.clients.values())
            self.clients.clear()
        
        for sock in clients_copy:
            try: sock.close()
            except: pass
            
        print("[SCREEN SERVER] Stopped")