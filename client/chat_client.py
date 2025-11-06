# client/chat_client.py
"""
Chat Client - Sends and receives text messages via TCP
"""
import socket
import threading
import struct
import json
from datetime import datetime
import sys
import time # Import time for potential delays/sleeps
from queue import Queue, Empty # Import Empty for queue handling if needed later
import os # Import os for path joining
# Ensure utils path is correct relative to this file
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.config import *


class ChatClient:
    # --- DEFINITIVE CORRECT __init__ ORDER ---
    def __init__(self, client_id, server_ip, username):
    # ------------------------------------------
        self.client_id = client_id
        self.username = username
        self.server_ip = server_ip # Use the correctly passed server_ip
        self.disconnect_callback = None
        self.server_port = CHAT_PORT
        self.callback = None
        self.sock = None
        self.running = False
        self.connected = False
        self.history_lock = threading.Lock()
        self.message_history = [] # Consider limiting size if memory is a concern
        self.message_callbacks = []
        self.receive_thread = None
        self.messages_sent = 0
        self.messages_received = 0

    def connect(self):
        """Connect to the chat server."""
        if self.connected: return True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Optional: Set TCP keepalive options
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

            self.sock.settimeout(10.0) # Connection timeout
            print(f"[CHAT CLIENT] Connecting to {self.server_ip}:{self.server_port}...") # Use self.server_ip
            self.sock.connect((self.server_ip, self.server_port)) # Use self.server_ip

            username_bytes = self.username.encode('utf-8')
            # Ensure username length fits in one byte (0-255)
            if len(username_bytes) > 255:
                username_bytes = username_bytes[:255]
                print("[CHAT CLIENT WARN] Username truncated to 255 bytes.")

            # Handshake: [4:client_id][1:username_len][username]
            handshake = struct.pack('!IB', self.client_id, len(username_bytes)) + username_bytes
            self.sock.sendall(handshake)

            # Receive acknowledgment [4:client_id][2:'OK']
            self.sock.settimeout(5.0) # Ack timeout
            ack = self._recv_exact(6) # Expect exactly 6 bytes

            # Check if ack is valid
            if ack and len(ack) == 6:
                ack_id = struct.unpack('!I', ack[:4])[0]
                ack_status = ack[4:]
                if ack_id == self.client_id and ack_status == b'OK':
                    self.connected = True
                    self.running = True
                    self.sock.settimeout(None) # Set back to blocking for receive loop or use keepalive
                    self.receive_thread = threading.Thread(target=self._receive_messages, daemon=True)
                    self.receive_thread.start()
                    print(f"[CHAT CLIENT] Connected as {self.username}")
                    return True
                else:
                    print(f"[CHAT CLIENT ERROR] Invalid acknowledgment received: ID={ack_id}, Status={ack_status}")
            else:
                print(f"[CHAT CLIENT ERROR] Failed to receive valid acknowledgment (Received: {ack})")

            # If connection failed or ack invalid, close socket
            self.sock.close()
            self.sock = None
            return False

        except socket.gaierror as e: # Specific error for getaddrinfo
            print(f"[CHAT CLIENT ERROR] Address error connecting to {self.server_ip}: {e}")
            if self.sock: self.sock.close(); self.sock = None
            return False
        except socket.timeout:
            print(f"[CHAT CLIENT ERROR] Connection timed out connecting to {self.server_ip}:{self.server_port}")
            if self.sock: self.sock.close(); self.sock = None
            return False
        except ConnectionRefusedError:
            print(f"[CHAT CLIENT ERROR] Connection refused by {self.server_ip}:{self.server_port}. Is the server running?")
            if self.sock: self.sock.close(); self.sock = None
            return False
        except Exception as e:
            print(f"[CHAT CLIENT ERROR] Connection failed: {e}")
            # Ensure socket is closed on any exception during connect
            if self.sock:
                try: self.sock.close()
                except: pass
            self.sock = None
            return False

    def _receive_messages(self):
        """Receive messages from server in a loop."""
        print("[CHAT CLIENT] Receiver thread started")
        while self.running:
            try:
                # Receive message length (4 bytes, unsigned int, network order)
                length_data = self._recv_exact(4)
                if not length_data:
                    print("[CHAT CLIENT INFO] Server disconnected (no length data).")
                    if self.disconnect_callback:
                        self.disconnect_callback()
                    break # Server closed connection

                msg_length = struct.unpack('!I', length_data)[0]

                # Basic sanity check for message length
                if msg_length == 0: continue # Skip empty messages
                if msg_length > 1024 * 10: # Limit message size (e.g., 10KB)
                    print(f"[CHAT CLIENT WARN] Received oversized message ({msg_length} bytes). Discarding.")
                    # Skip reading the oversized message to avoid issues
                    # This requires careful handling or just disconnecting
                    # For simplicity, let's try to read and discard if possible, or break
                    try: self._recv_exact(msg_length) # Attempt to read and discard
                    except: break # Disconnect if reading fails badly
                    continue

                # Receive message data
                msg_data = self._recv_exact(msg_length)
                if not msg_data:
                    if self.disconnect_callback:
                        self.disconnect_callback()
                    print("[CHAT CLIENT INFO] Server disconnected (no message data).")
                    break

                # Decode and parse message
                try:
                    message = json.loads(msg_data.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print("[CHAT CLIENT WARN] Received invalid message format. Skipping.")
                    continue

                # Process valid message
                with self.history_lock:
                    self.message_history.append(message)
                    # Optional: Limit history size
                    if len(self.message_history) > 200:
                        self.message_history = self.message_history[-150:]
                self.messages_received += 1

                # Trigger callbacks (pass a copy)
                self._notify_callbacks(message.copy())

            except (ConnectionAbortedError, ConnectionResetError, OSError) as e:
                print(f"[CHAT CLIENT INFO] Connection lost: {e}")
                if self.disconnect_callback:
                    self.disconnect_callback()
                break # Exit loop on connection errors
            except Exception as e:
                # Log unexpected errors but try to continue if self.running is still True
                if self.running:
                    print(f"[CHAT CLIENT ERROR] Receive error: {e}")
                    # Consider adding a small sleep to prevent rapid error loops
                    time.sleep(0.1)
                else:
                    break # Exit if disconnect was initiated

        # Cleanup after loop ends
        self.connected = False
        self.running = False # Ensure running flag is false
        print("[CHAT CLIENT] Receiver thread stopped")
        # Notify GUI or main thread that connection is lost (optional)
        self._notify_callbacks({"type": "system", "text": "Disconnected from server."})

    def _recv_exact(self, length):
        """Receive exactly 'length' bytes or return None on failure."""
        if not self.sock: return None
        data = b''
        try:
            # No explicit timeout here, rely on TCP keepalive or higher-level timeouts
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk: # Socket closed gracefully by peer
                    return None
                data += chunk
            return data
        except (socket.timeout, ConnectionError, OSError):
            # Includes ConnectionResetError, ConnectionAbortedError
            return None # Treat timeout/errors as connection failure for recv

    def send_message(self, text, recipient_id=None):
        """Send a text message to the server. If recipient_id is provided, sends as private message."""
        if not self.connected or not self.sock:
            print("[CHAT CLIENT] Cannot send: Not connected")
            return False
        text = text.strip()
        if not text: return True # Don't send empty messages

        try:
            message = {'text': text[:512]} # Limit outgoing message size
            # Add recipient_id if this is a private message
            if recipient_id is not None:
                message['recipient_id'] = recipient_id
                message['is_private'] = True
            msg_json = json.dumps(message).encode('utf-8')
            msg_packet = struct.pack('!I', len(msg_json)) + msg_json
            self.sock.sendall(msg_packet)
            self.messages_sent += 1
            return True
        except (OSError, ConnectionError) as e:
            print(f"[CHAT CLIENT ERROR] Send failed (connection lost): {e}")
            self.disconnect() # Trigger disconnect sequence
            return False
        except Exception as e:
            print(f"[CHAT CLIENT ERROR] Send failed: {e}")
            return False

    def send_reaction(self, emoji):
        """Send a reaction emoji to the server."""
        if not self.connected or not self.sock:
            print("[CHAT CLIENT] Cannot send reaction: Not connected")
            return False

        try:
            message = {'type': 'reaction', 'emoji': emoji}
            msg_json = json.dumps(message).encode('utf-8')
            msg_packet = struct.pack('!I', len(msg_json)) + msg_json
            self.sock.sendall(msg_packet)
            return True
        except (OSError, ConnectionError) as e:
            print(f"[CHAT CLIENT ERROR] Send reaction failed (connection lost): {e}")
            self.disconnect()
            return False
        except Exception as e:
            print(f"[CHAT CLIENT ERROR] Send reaction failed: {e}")
            return False

    def register_callback(self, callback):
        """Register a callback function for new messages."""
        if callback not in self.message_callbacks:
            self.message_callbacks.append(callback)

    def register_disconnect_callback(self, callback):
        """Register a callback function to be called on disconnect."""
        self.disconnect_callback = callback

    def _notify_callbacks(self, message):
        """Notify all registered callbacks of a new message."""
        for callback in self.message_callbacks:
            try:
                callback(message)
            except Exception as e:
                print(f"[CHAT CLIENT] Callback error: {e}")

    def get_message_history(self):
        """Get a copy of the message history."""
        with self.history_lock:
            return list(self.message_history)

    def disconnect(self):
        """Disconnect from the server and clean up resources."""
        print("[CHAT CLIENT] Disconnecting...")
        if not self.running and not self.connected: return # Already disconnected
        self.running = False
        self.connected = False

        sock = self.sock # Use local variable for safety in threads
        self.sock = None
        if sock:
            try:
                # Shutdown sending first, then receiving
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass # Ignore if socket already closed/broken
            try:
                sock.close()
            except OSError:
                pass # Ignore if already closed

        # Wait briefly for receive thread to exit
        if self.receive_thread and self.receive_thread.is_alive():
            self.receive_thread.join(timeout=0.5)

        print(f"[CHAT CLIENT] Stats - Sent: {self.messages_sent}, Received: {self.messages_received}")