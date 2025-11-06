# server/main_server.py
"""
Main Server - Integrates all server components
Manages video, audio, chat, file, and screen sharing services
"""

import sys
import time
import signal
import os

# Ensure project root is on sys.path before importing local packages
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from utils.config import *
from video_server import VideoServer
from audio_server import AudioServer
from chat_server import ChatServer
from file_server import FileServer
from screen_server import ScreenServer

class MainServer:
    def __init__(self):
        self.video_server = None
        self.audio_server = None
        self.chat_server = None
        self.file_server = None
        self.screen_server = None
        self.running = False
        
    def start(self):
        """Start all server components"""
        print("=" * 60)
        print("LAN COMMUNICATION SERVER")
        print("=" * 60)
        
        try:
            print("\n[1/5] Starting Video Server...")
            self.video_server = VideoServer()
            if not self.video_server.start():
                print("[ERROR] Failed to start Video Server")
                return False
            
            print("\n[2/5] Starting Audio Server...")
            self.audio_server = AudioServer()
            if not self.audio_server.start():
                print("[ERROR] Failed to start Audio Server")
                self.stop()
                return False
            
            print("\n[3/5] Starting Chat Server...")
            self.chat_server = ChatServer()
            if not self.chat_server.start():
                print("[ERROR] Failed to start Chat Server")
                self.stop()
                return False
            
            print("\n[4/5] Starting File Server...")
            self.file_server = FileServer()
            if not self.file_server.start():
                print("[ERROR] Failed to start File Server")
                self.stop()
                return False

            print("\n[5/5] Starting Screen Sharing Server...")
            self.screen_server = ScreenServer()
            if not self.screen_server.start():
                print("[ERROR] Failed to start Screen Server")
                self.stop()
                return False

        except Exception as e:
            print(f"[FATAL] An unexpected error occurred during server startup: {e}")
            import traceback
            traceback.print_exc()
            self.stop()
            return False
        
        self.running = True
        print("\n" + "=" * 60)
        print("ALL SERVERS RUNNING")
        print("=" * 60)
        print(f"Video Server:  UDP Port {VIDEO_PORT}")
        print(f"Audio Server:  UDP Port {AUDIO_PORT}")
        print(f"Chat Server:   TCP Port {CHAT_PORT}")
        print(f"File Server:   TCP Port {FILE_PORT}")
        print(f"Screen Server: TCP Port {SCREEN_PORT}")
        print("=" * 60)
        print("\nPress Ctrl+C to stop all servers\n")
        
        return True
    
    def print_stats(self):
        """Print statistics from all servers"""
        print("\n" + "=" * 60)
        print("SERVER STATISTICS")
        print("=" * 60)
        
        if self.video_server:
            video_stats = self.video_server.get_stats() 
            print(f"\n[VIDEO] Active clients: {video_stats['active_clients']}")
            for client_id, stats in video_stats['client_stats'].items():
                print(f"  Client {client_id}: {stats['packets']} packets, {stats['bytes']} bytes")
        
        if self.audio_server:
            audio_stats = self.audio_server.get_stats() 
            print(f"\n[AUDIO] Active clients: {audio_stats['active_clients']}")
            for client_id, stats in audio_stats['client_stats'].items():
                print(f"  Client {client_id}: {stats['packets']} packets, {stats['bytes']} bytes")
        
        if self.chat_server:
            chat_stats = self.chat_server.get_stats()
            print(f"\n[CHAT] Active clients: {chat_stats['active_clients']}")
            print(f"       Total messages: {chat_stats['total_messages']}")
            for client_id, info in chat_stats['clients'].items():
                print(f"  Client {client_id} ({info['username']}): {info['address']}")
        
        if self.file_server:
            file_stats = self.file_server.get_stats()
            print(f"\n[FILE] Active clients: {file_stats['active_clients']}")
            print(f"       Total files: {file_stats['total_files']}")
            print(f"       Available files: {file_stats['available_files']}")
            print(f"       Total bytes: {file_stats['total_bytes']}")
        
        print("=" * 60)
    
    def stop(self):
        """Stop all server components"""
        print("\n\nShutting down servers...")
        self.running = False
        
        # Stop servers in reverse order of start
        if self.screen_server: self.screen_server.stop()
        if self.file_server: self.file_server.stop()
        if self.chat_server: self.chat_server.stop()
        if self.audio_server: self.audio_server.stop()
        if self.video_server: self.video_server.stop()
        
        print("\nAll servers stopped. Goodbye!")


# --- FIX: The signal_handler function has been removed ---


if __name__ == "__main__":
    main_server = MainServer()
    
    # --- FIX: The signal.signal() call has been removed ---
    
    if main_server.start():
        try:
            while main_server.running:
                time.sleep(1)
                # This loop now just keeps the main thread alive.
                # All the logic is handled in the 'except' and 'finally' blocks.
        except KeyboardInterrupt:
            # This block will now execute when you press Ctrl+C
            print("\n\nCtrl+C received. Shutting down gracefully...")
        finally:
            # This block is GUARANTEED to run, ensuring a clean shutdown.
            main_server.stop()
    else:
        print("\nFailed to start one or more servers. Exiting...")
        sys.exit(1)