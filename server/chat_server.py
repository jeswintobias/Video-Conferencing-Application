# server/chat_server.py
"""
Chat Server - Final version ensuring reliable user list broadcasting.
"""
import socket
import threading
import struct
import time
import json
from datetime import datetime
import sys
sys.path.append('..')
from utils.config import *

class ChatServer:
    def __init__(self, host=SERVER_HOST, port=CHAT_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.running = False
        self.clients = {} # {client_id: {'socket': sock, 'address': addr, 'username': name}}
        self.client_lock = threading.Lock()
        self.message_history = []
        self.history_lock = threading.Lock()
        self.total_messages = 0

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.host, self.port))
            self.sock.listen(MAX_CLIENTS)
            self.running = True
            print(f"[CHAT SERVER] Started on {self.host}:{self.port}")
            accept_thread = threading.Thread(target=self._accept_connections, daemon=True)
            accept_thread.start()
            return True
        except Exception as e:
            print(f"[CHAT SERVER ERROR] Failed to start: {e}")
            return False

    def _accept_connections(self):
        while self.running:
            try:
                client_sock, client_addr = self.sock.accept()
                # Set keepalive options
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
                handler_thread = threading.Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True)
                handler_thread.start()
            except OSError:
                if self.running: print("[CHAT SERVER] Socket closed, stopping accept loop.")
                break
            except Exception as e:
                if self.running: print(f"[CHAT SERVER ERROR] Accept error: {e}")

    def _handle_client(self, client_sock, client_addr):
        client_id = None
        username = None
        try:
            client_sock.settimeout(10.0) # Handshake timeout
            handshake = self._recv_exact(client_sock, 5) # Min length: 4 (ID) + 1 (len byte)
            if not handshake: return

            client_id = struct.unpack('!I', handshake[:4])[0]
            username_len = handshake[4]
            if username_len > 0:
                 username_bytes = self._recv_exact(client_sock, username_len)
                 if not username_bytes: return
                 username = username_bytes.decode('utf-8', errors='ignore')
            else:
                 username = f"User_{client_id}" # Fallback username

            # Add client under lock
            with self.client_lock:
                if client_id in self.clients:
                    # A client with this ID is already connected. Reject this new connection.
                    print(f"[CHAT SERVER WARN] Client {client_id} ({username}) tried to connect while already active. Rejecting.")
                    try:
                        # Send a "busy" or "error" message? For now, just close.
                        client_sock.close()
                    except:
                        pass
                    return # Exit the handler thread for this new, rejected socket
                
                # If not present, add them
                self.clients[client_id] = {'socket': client_sock, 'address': client_addr, 'username': username}
            
            print(f"[CHAT SERVER] Client {client_id} ({username}) connected from {client_addr}")

            # Send ack and history (history includes system messages)
            self._send_ack(client_sock, client_id)
            self._send_history(client_sock)

            # --- Critical Sync Point ---
            # Broadcast join message AND updated user list AFTER adding the client
            self._broadcast_system_message(f"{username} joined the chat.")
            self._broadcast_user_list() # Ensures everyone, including the joiner, gets the list

            client_sock.settimeout(None) # Longer timeout for receiving messages

            while self.running:
                 # Check connection before blocking read
                 with self.client_lock:
                    if client_id not in self.clients or self.clients[client_id]['socket'] != client_sock:
                         print(f"[CHAT SERVER DEBUG] Socket mismatch or client removed for {client_id}. Exiting loop.")
                         break

                 length_data = self._recv_exact(client_sock, 4)
                 if not length_data: break # Connection closed gracefully

                 msg_length = struct.unpack('!I', length_data)[0]
                 # Add sanity check for message length
                 if msg_length > 4096: # Limit message size
                      print(f"[CHAT SERVER WARN] Client {client_id} sent oversized message ({msg_length} bytes). Disconnecting.")
                      break
                 if msg_length == 0: continue # Ignore empty messages

                 msg_data = self._recv_exact(client_sock, msg_length)
                 if not msg_data: break

                 try:
                    message = json.loads(msg_data.decode('utf-8'))
                    self._process_message(client_id, username, message)
                 except (json.JSONDecodeError, UnicodeDecodeError):
                      print(f"[CHAT SERVER WARN] Client {client_id} sent invalid message format.")
                      continue # Don't disconnect for bad message, just ignore

        except (socket.timeout, ConnectionError, OSError) as e:
            print(f"[CHAT SERVER INFO] Connection issue with {client_id if client_id else client_addr}: {e}")
        except Exception as e:
            print(f"[CHAT SERVER ERROR] Unexpected error handling {client_id if client_id else client_addr}: {e}")
        finally:
            print(f"[CHAT SERVER] Cleaning up connection for {client_id if client_id else client_addr}")
            if client_id:
                client_left = False
                with self.client_lock:
                    if client_id in self.clients and self.clients[client_id]['socket'] == client_sock:
                        removed_client = self.clients.pop(client_id)
                        username = removed_client['username'] # Get final username
                        client_left = True
                if client_left:
                    print(f"[CHAT SERVER] Client {client_id} ({username}) disconnected.")
                    # --- Critical Sync Point ---
                    self._broadcast_system_message(f"{username} left the chat.")
                    self._broadcast_user_list() # Update list for everyone else
            try: client_sock.close()
            except: pass

    def _recv_exact(self, sock, length):
        data = b''
        try:
            while len(data) < length:
                chunk = sock.recv(length - len(data))
                if not chunk: return None # Connection closed
                data += chunk
            return data
        except (socket.timeout, ConnectionError, OSError):
            return None # Treat errors during recv as closed connection

    def _send_ack(self, client_sock, client_id):
        try:
            ack_msg = struct.pack('!I', client_id) + b'OK'
            client_sock.sendall(ack_msg)
        except Exception as e:
             print(f"[CHAT SERVER WARN] Failed to send ACK to {client_id}: {e}")


    def _send_history(self, client_sock):
        with self.history_lock:
            # Send recent messages (limit history size)
            history_to_send = self.message_history[-50:]
        for msg in history_to_send:
            if not self._send_message_to_socket(client_sock, msg):
                break # Stop sending history if connection breaks


    def _process_message(self, client_id, username, message):
        text = message.get('text', '').strip()
        if not text: return # Ignore empty messages

        msg_obj = {
            'type': 'message', 'client_id': client_id, 'username': username,
            'text': text[:512], # Limit message length
            'timestamp': datetime.now().isoformat()
        }
        with self.history_lock:
            self.message_history.append(msg_obj)
            # Trim history occasionally
            if len(self.message_history) > 200:
                 self.message_history = self.message_history[-150:]

        self.total_messages += 1
        print(f"[CHAT] {username}: {msg_obj['text']}")
        self._broadcast_message(msg_obj)


    def _send_message_to_socket(self, sock, msg_obj):
        """Sends a single message object; returns True on success, False on failure."""
        try:
            msg_json = json.dumps(msg_obj).encode('utf-8')
            msg_packet = struct.pack('!I', len(msg_json)) + msg_json
            sock.sendall(msg_packet)
            return True
        except (OSError, ConnectionError) as e:
            # print(f"[CHAT SERVER DEBUG] Failed to send message (socket likely closed): {e}")
            return False
        except Exception as e:
             print(f"[CHAT SERVER WARN] Unexpected error sending message: {e}")
             return False


    def _broadcast_message(self, msg_obj, exclude_client_id=None):
        """Broadcasts a message object to all connected clients except excluded."""
        disconnected_clients = []
        with self.client_lock:
            # Iterate over a copy to allow modification if send fails
            current_clients = list(self.clients.items())

        # Send outside the lock
        for cid, info in current_clients:
            if cid == exclude_client_id: continue
            if not self._send_message_to_socket(info['socket'], msg_obj):
                disconnected_clients.append(cid) # Mark for cleanup

        # Cleanup disconnected clients if any send failed
        if disconnected_clients:
             client_left = False
             with self.client_lock:
                 for cid in disconnected_clients:
                     if cid in self.clients:
                         print(f"[CHAT SERVER INFO] Client {cid} disconnected during broadcast.")
                         try: self.clients[cid]['socket'].close()
                         except: pass
                         del self.clients[cid]
                         client_left = True
             # If someone left, update the user list for remaining clients
             if client_left:
                 self._broadcast_user_list()


    def _broadcast_system_message(self, text):
        msg_obj = {'type': 'system', 'text': text, 'timestamp': datetime.now().isoformat()}
        with self.history_lock:
            self.message_history.append(msg_obj)
        self._broadcast_message(msg_obj)


    def _broadcast_user_list(self):
        """Constructs and broadcasts the current user list reliably."""
        print("[CHAT SERVER] Broadcasting updated user list...")
        with self.client_lock:
            user_list = [{"client_id": cid, "username": info["username"]} for cid, info in self.clients.items()]
        
        msg_obj = {"type": "user_list", "users": user_list, "timestamp": datetime.now().isoformat()}
        self._broadcast_message(msg_obj) # Broadcast to all current clients


    def get_stats(self):
        with self.client_lock:
            return {
                'active_clients': len(self.clients),
                'total_messages': self.total_messages,
                'clients': {cid: {'username': info['username'], 'address': info['address']} for cid, info in self.clients.items()}
            }

    def stop(self):
        print("[CHAT SERVER] Stopping...")
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

        print("[CHAT SERVER] Stopped")