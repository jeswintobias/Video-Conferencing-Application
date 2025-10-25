# client/audio_client.py
"""
Audio Client - Definitive stable version for Windows.
- Robust shutdown sequence to prevent crashes.
- Includes heartbeat for connection stability.
- [FIXED] Added robust error handling in playback loop.
"""
import socket
import threading
import struct
import time
import numpy as np
import pyaudio
from queue import Queue, Empty
import sys
sys.path.append('..')
from utils.config import *

class AudioClient:
    def __init__(self, client_id, server_ip):
        self.client_id = client_id
        self.server_ip = server_ip
        self.server_port = AUDIO_PORT
        self.sock = None
        self.running = True # Control flag for all threads
        self.sending = False
        self.receiving = False
        self._audio_interface = None # Hold PyAudio instance
        self.input_stream = None
        self.output_stream = None
        self._threads = [] # Keep track of threads for clean shutdown
        self.playback_queue = Queue(maxsize=50)
        self.seq_num = 0
        self.packets_sent = 0
        self.packets_received = 0

    def connect(self):
        try:
            self._audio_interface = pyaudio.PyAudio() # Initialize PyAudio here
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(1.0) # Non-blocking receive

            # Start threads and add to list
            recv_thread = threading.Thread(target=self._receive_audio, daemon=True)
            play_thread = threading.Thread(target=self._playback_audio, daemon=True)
            hb_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
            self._threads.extend([recv_thread, play_thread, hb_thread])

            recv_thread.start()
            play_thread.start()
            hb_thread.start()

            self._send_hello_packet()
            print(f"[AUDIO CLIENT] Connected to {self.server_ip}:{self.server_port}")
            return True
        except Exception as e:
            print(f"[AUDIO CLIENT ERROR] Connection failed: {e}")
            self.running = False # Ensure flag is false on error
            self._cleanup_audio_interface() # Clean up PyAudio if connect fails
            return False

    def start_microphone(self, input_device_index=None):
        if self.sending or not self.running: return True
        try:
            # Ensure audio interface exists
            if not self._audio_interface: self._audio_interface = pyaudio.PyAudio()

            self.input_stream = self._audio_interface.open(
                format=pyaudio.paInt16, channels=1, rate=AUDIO_RATE,
                input=True, input_device_index=input_device_index, frames_per_buffer=AUDIO_CHUNK
            )
            self.sending = True
            send_thread = threading.Thread(target=self._send_audio, daemon=True)
            self._threads.append(send_thread) # Track send thread
            send_thread.start()
            print("[AUDIO CLIENT] Microphone started")
            return True
        except Exception as e:
            print(f"[AUDIO CLIENT ERROR] Microphone start failed: {e}")
            self.input_stream = None # Ensure stream is None on failure
            return False

    def stop_microphone(self):
        if not self.sending: return
        self.sending = False # Signal send thread to stop
        # Let the send thread finish naturally, don't join here

        stream = self.input_stream
        self.input_stream = None # Set to None immediately
        if stream:
            try:
                # Check is_active() *before* stopping/closing
                is_active = False
                try:
                    is_active = stream.is_active()
                except OSError: # Catch if stream is already invalid
                    pass

                if is_active:
                    stream.stop_stream()
                stream.close()
            except Exception as e:
                print(f"[AUDIO CLIENT DEBUG] Error closing input stream: {e}")
        print("[AUDIO CLIENT] Microphone stopped")

    def start_speakers(self, output_device_index=None):
        if self.receiving or not self.running: return True
        try:
            if not self._audio_interface: self._audio_interface = pyaudio.PyAudio()

            self.output_stream = self._audio_interface.open(
                format=pyaudio.paInt16, channels=1, rate=AUDIO_RATE,
                output=True, output_device_index=output_device_index, frames_per_buffer=AUDIO_CHUNK
            )
            self.receiving = True
            print("[AUDIO CLIENT] Speakers started")
            return True
        except Exception as e:
            print(f"[AUDIO CLIENT ERROR] Speakers start failed: {e}")
            self.output_stream = None
            return False

    def stop_speakers(self):
        if not self.receiving: return
        self.receiving = False # Signal playback thread

        stream = self.output_stream
        self.output_stream = None # Set to None immediately
        if stream:
            try:
                # Check is_active() *before* stopping/closing
                is_active = False
                try:
                    is_active = stream.is_active()
                except OSError: # Catch if stream is already invalid
                    pass

                if is_active:
                    stream.stop_stream()
                stream.close()
            except Exception as e:
                print(f"[AUDIO CLIENT DEBUG] Error closing output stream: {e}")
        print("[AUDIO CLIENT] Speakers stopped")

    def _send_hello_packet(self):
        try:
            packet = struct.pack('!BI', 255, self.client_id)
            for _ in range(3):
                if not self.running: break # Check running flag
                self.sock.sendto(packet, (self.server_ip, self.server_port))
                time.sleep(0.01)
        except Exception as e:
            if self.running: print(f"[AUDIO CLIENT ERROR] Failed to send HELLO packet: {e}")

    def _send_heartbeat(self):
        while self.running:
            try:
                time.sleep(10)
                if self.running and not self.sending: # Only send heartbeat if not sending audio
                    packet = struct.pack('!BI', 254, self.client_id)
                    self.sock.sendto(packet, (self.server_ip, self.server_port))
            except Exception as e:
                if self.running: print(f"[AUDIO CLIENT ERROR] Heartbeat failed: {e}")

    def _send_audio(self):
        while self.running and self.sending:
            try:
                stream = self.input_stream # Use local variable for safety
                # Check stream validity *before* reading
                if not stream:
                    print("[AUDIO CLIENT DEBUG] Input stream is None in send loop.")
                    break
                # Add a try-except around is_active()
                try:
                    if not stream.is_active():
                        print("[AUDIO CLIENT DEBUG] Input stream is not active in send loop.")
                        break
                except OSError:
                     print("[AUDIO CLIENT DEBUG] Input stream check failed (OSError) in send loop.")
                     break

                audio_data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                packet = struct.pack('!BII', 2, self.client_id, self.seq_num) + audio_data
                self.sock.sendto(packet, (self.server_ip, self.server_port))
                self.seq_num = (self.seq_num + 1) % (2**32) # Prevent overflow
                self.packets_sent += 1
            except IOError as e:
                # This can happen if the device is disconnected
                print(f"[AUDIO CLIENT ERROR] Microphone read error: {e}")
                self.sending = False # Signal loop stop
                break
            except Exception as e:
                if self.running and self.sending:
                    print(f"[AUDIO CLIENT ERROR] Send error: {e}")
                break # Exit on other errors too
        self.sending = False # Ensure sending is False when loop exits
        print("[AUDIO CLIENT DEBUG] Send audio loop finished.")


    def _receive_audio(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                if data and data[0] == 2 and len(data) > 9: # Check for audio packet type 2
                    sender_id = struct.unpack('!I', data[1:5])[0] # Get sender ID if needed later
                    # seq_num_recv = struct.unpack('!I', data[5:9])[0] # Get seq num if needed
                    audio_data = data[9:]
                    # Basic validation: ensure data length matches expected chunk size
                    if len(audio_data) == AUDIO_CHUNK * AUDIO_CHANNELS * 2: # 2 bytes per sample (paInt16)
                        if not self.playback_queue.full():
                            self.playback_queue.put(audio_data)
                        # else:
                            # print("[AUDIO CLIENT WARN] Playback queue full, dropping packet.")
                        self.packets_received += 1
            except socket.timeout:
                continue # Normal when no data is received
            except Exception as e:
                if self.running: print(f"[AUDIO CLIENT ERROR] Receive error: {e}")

    # --- UPDATED PLAYBACK FUNCTION ---
    def _playback_audio(self):
        """Plays audio; includes refined safety checks for shutdown."""
        silence = np.zeros(AUDIO_CHUNK * AUDIO_CHANNELS, dtype=np.int16).tobytes()
        while self.running:
            stream = self.output_stream # Use local variable for safety
            
            # Check primary conditions first
            if not self.running or not self.receiving or not stream:
                time.sleep(0.02) # Wait briefly if not ready or stopped
                continue

            try:
                # --- ROBUST STREAM CHECK ---
                # Check if stream is active *inside* the loop and try-except
                if not stream.is_active():
                    # Stream might have been stopped by another thread
                    time.sleep(0.02)
                    continue
                # --- END ROBUST CHECK ---

                try:
                    audio_data = self.playback_queue.get(timeout=0.02)
                    stream.write(audio_data)
                except Empty:
                    # Write silence only if the stream is still active
                    if stream.is_active():
                        stream.write(silence)
                
            except (OSError, IOError, AttributeError) as e:
                # Catch errors related to stream state (closed, invalid, etc.)
                # This includes the "Stream not open" error
                if self.running: # Avoid printing errors during intentional shutdown
                     print(f"[AUDIO CLIENT ERROR] Playback stream error: {e}")
                # Assume the stream is bad, stop trying to receive/play
                self.receiving = False 
                # Attempt to close the faulty stream instance if it still exists locally
                if stream: 
                    try:
                        stream.close()
                    except: pass 
                self.output_stream = None # Ensure the main reference is cleared
                time.sleep(0.1) # Wait a bit before potentially restarting
                
            except Exception as e:
                # Catch any other unexpected errors
                 if self.running: 
                     print(f"[AUDIO CLIENT ERROR] Unexpected playback error: {e}")
                 time.sleep(0.1)

        print("[AUDIO CLIENT DEBUG] Playback thread finished.") # Debug message
    # --- END UPDATED PLAYBACK FUNCTION ---

    def _cleanup_audio_interface(self):
        """Safely terminates the PyAudio interface."""
        if self._audio_interface:
            try:
                self._audio_interface.terminate()
            except Exception as e:
                print(f"[AUDIO CLIENT DEBUG] Error terminating PyAudio: {e}")
            self._audio_interface = None

    def disconnect(self):
        print("[AUDIO CLIENT] Disconnecting...")
        if not self.running: return
        self.running = False # Signal all threads to stop FIRST

        # Close streams immediately using the stop methods
        self.stop_microphone()
        self.stop_speakers()

        # Close the socket to interrupt blocking calls in threads like recvfrom
        sock = self.sock
        self.sock = None
        if sock:
            # Closing the socket is generally enough for UDP
            try:
                sock.close()
            except Exception as e:
                 print(f"[AUDIO CLIENT DEBUG] Error closing socket: {e}")


        # Wait briefly for threads to exit AFTER signaling stop and closing resources
        active_threads = [t for t in self._threads if t and t.is_alive()]
        for t in active_threads:
            t.join(timeout=0.2) # Wait a short time for clean exit

        # Clean up PyAudio instance *last*
        self._cleanup_audio_interface()

        print(f"[AUDIO CLIENT] Disconnected. Sent: {self.packets_sent}, Received: {self.packets_received}")