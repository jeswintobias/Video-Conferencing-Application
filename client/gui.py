# client/gui.py
"""
Main GUI Application - Final Definitive Version.
- Fixes participant synchronization issues.
- Reliably clears frozen video frames.
- Robust error handling and state management.
- Stable shutdown sequence.
- [FIXED] Removes client-side message duplication in chat.
- [FIXED] Prevents "hall of mirrors" loop by correctly using the QStackedWidget.
- [UI_UPDATE] Replaces control bar with modern icons from `qtawesome`.
- [UI_UPDATE] Adds a Hangup/End Call button.
"""
import sys
import os
import time
import random
import cv2
import math
from queue import Empty
import threading
import traceback # For detailed error logging
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QTextEdit, QLineEdit,
                             QGridLayout, QFileDialog, QProgressBar, QMessageBox,
                             QInputDialog, QSplitter, QScrollArea, QListWidgetItem, QListWidget,
                             QTabWidget, QStackedWidget, QSizePolicy, QStyle)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QSize, QRect
from PyQt5.QtGui import (QImage, QPixmap, QFont, QPainter, QColor,
                         QIcon)
import qtawesome # <-- IMPORT QTAWESOME

# Use a robust path finding mechanism
try:
    from video_client import VideoClient
    from audio_client import AudioClient
    from chat_client import ChatClient
    from file_client import FileClient
    from screen_client import ScreenClient
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils.config import *
except ImportError as e:
    print(f"FATAL ERROR: Could not import client modules or config: {e}")
    print("Ensure all .py files are present and utils/config.py exists.")
    sys.exit(1)


class ParticipantTile(QWidget):
    """Final robust ParticipantTile widget."""
    def __init__(self, username="", client_id=None, parent=None):
        super().__init__(parent)
        self.username = username
        self.client_id = client_id
        self.setMinimumSize(200, 112) # 16:9 base
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pixmap = None
        self._has_frame = False

    def resizeEvent(self, event):
        if self.width() > 0:
            new_height = int(self.width() * 9 / 16)
            if new_height > 0: self.setFixedHeight(new_height)
        super().resizeEvent(event)

    def update_frame(self, frame_bgr):
        try:
            h, w, _ = frame_bgr.shape
            if h <= 0 or w <= 0: self.clear_frame(); return
            rgb_image = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            qt_image = QImage(rgb_image.data, w, h, w * 3, QImage.Format_RGB888)
            new_pixmap = QPixmap.fromImage(qt_image)
            # Check if pixmap is valid before updating
            if not new_pixmap.isNull():
                # Update only if state changes or pixmap content differs (basic check)
                if not self._has_frame or self._pixmap is None or new_pixmap.cacheKey() != self._pixmap.cacheKey():
                    self._pixmap = new_pixmap
                    self._has_frame = True
                    self.update() # Request repaint only if changed
            else: # Invalid pixmap generated
                if self._has_frame: self._has_frame = False; self.update()
        except Exception as e:
            # print(f"DEBUG: Error updating frame for {self.client_id}: {e}")
            if self._has_frame: self._has_frame = False; self.update()

    def clear_frame(self):
        # Only update if the state is actually changing
        if self._has_frame:
            self._has_frame = False
            self._pixmap = None
            self.update() # Request repaint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#202225"))
        target_rect = self.rect().adjusted(2, 2, -2, -2)

        if self._has_frame and self._pixmap and not self._pixmap.isNull():
            pixmap_scaled = self._pixmap.scaled(target_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = target_rect.left() + (target_rect.width() - pixmap_scaled.width()) // 2
            y = target_rect.top() + (target_rect.height() - pixmap_scaled.height()) // 2
            painter.drawPixmap(x, y, pixmap_scaled)
        else: # Draw initials placeholder
            painter.setPen(QColor("#8a8d91"))
            font_size = max(10, int(self.height() * 0.2))
            font = QFont("Segoe UI", font_size, QFont.Bold)
            painter.setFont(font)
            initials = self._make_initials(self.username)
            painter.drawText(self.rect(), Qt.AlignCenter, initials)

        # Draw name overlay
        name_bar_height = max(20, int(self.height() * 0.15))
        painter.fillRect(0, self.height() - name_bar_height, self.width(), name_bar_height, QColor(0, 0, 0, 150))
        painter.setPen(Qt.white)
        font_size = max(8, int(name_bar_height * 0.5))
        font = QFont("Segoe UI", font_size)
        painter.setFont(font)
        display_name = self.username if self.username else f"User {self.client_id}"
        metrics = painter.fontMetrics()
        elided_text = metrics.elidedText(display_name, Qt.ElideRight, self.width() - 10)
        painter.drawText(QRect(5, self.height() - name_bar_height, self.width() - 10, name_bar_height), Qt.AlignVCenter | Qt.AlignLeft, elided_text)
        painter.end()

    def _make_initials(self, name):
        if not name: return "?"
        parts = name.replace("(You)", "").strip().split()
        if not parts: return "?"
        if len(parts) == 1: return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

class LANCommClient(QMainWindow):
    new_message_signal = pyqtSignal(dict)
    new_file_signal = pyqtSignal(dict)
    progress_signal = pyqtSignal(str, str, float, int, int)
    presenter_update_signal = pyqtSignal(object)
    _disconnecting = False

    def __init__(self):
        super().__init__()
        self.running = True
        self.client_id = random.randint(1000, 9999)
        self._skip_close_dialog = False
        self.clients = {}
        self.participant_tiles = {}
        self.client_connections = {}

        self.video_timer = QTimer(self); self.video_timer.timeout.connect(self.update_video_frames)
        self.screen_timer = QTimer(self); self.screen_timer.timeout.connect(self.update_screen_frame)

        # Connect all signals *before* any client can send one.
        self.new_message_signal.connect(self.on_new_message)
        self.new_file_signal.connect(self.on_new_file)
        self.progress_signal.connect(self.on_transfer_progress)
        self.presenter_update_signal.connect(self.on_presenter_update)

        # --- UI UPDATE: Store modern icons from qtawesome ---
        icon_color = 'white'
        icon_color_active = '#007bff' # Blue for active chat
        self.icon_video_off = qtawesome.icon('fa5s.video-slash', color=icon_color)
        self.icon_video_on = qtawesome.icon('fa5s.video', color=icon_color)
        self.icon_audio_off = qtawesome.icon('fa5s.microphone-slash', color=icon_color)
        self.icon_audio_on = qtawesome.icon('fa5s.microphone', color=icon_color)
        self.icon_screen_off = qtawesome.icon('fa5s.laptop', color=icon_color)
        self.icon_screen_on = qtawesome.icon('fa5s.stop-circle', color=icon_color)
        self.icon_chat = qtawesome.icon('fa5s.comments', color=icon_color)
        self.icon_chat_active = qtawesome.icon('fa5s.comments', color=icon_color_active)
        # --- ADD HANGUP ICON ---
        self.icon_hangup = qtawesome.icon('fa5s.phone-slash', color='white')
        # ---

        if not self.show_connection_dialog(): # Get username/IP
            self.running = False; sys.exit(0)

        self.init_ui() # Build UI
        connection_ok = self.connect_to_servers() # Connect

        if self.running and connection_ok:
            self.add_or_update_participant(self.client_id, f"{self.username} (You)")
            self.redraw_participant_grid()

            self.video_timer.start(int(1000 / VIDEO_FPS))
            self.screen_timer.start(int(1000 / SCREEN_FPS))

        elif self.running:
            QMessageBox.critical(self, "Connection Failed", "Could not connect. Check server IP and status.")
            self.running = False
            QTimer.singleShot(50, self.close)

    # --- Methods (show_connection_dialog, _create_chat_widget, _create_file_widget are unchanged) ---
    def show_connection_dialog(self):
        self.username, ok = QInputDialog.getText(self, 'Username', 'Enter username:');
        if not ok or not self.username.strip(): return False
        self.server_ip, ok = QInputDialog.getText(self, 'Server IP', 'Enter server IP:', text='127.0.0.1');
        if not ok or not self.server_ip.strip(): return False
        return True
    
    def hangup_and_close(self):
        """
        Sets a flag to skip the confirmation dialog and then
        triggers the window's close event.
        """
        print("[GUI] Hangup button clicked. Initiating immediate shutdown.")
        self._skip_close_dialog = True
        self.close()

    def init_ui(self):
        self.setWindowTitle(f'LAN Comm - {self.username}')
        self.setGeometry(100, 100, 1280, 720)
        self.setStyleSheet("background-color: #1e1e1e; color: #f0f0f0; border: none;")
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0,0,0,0); main_layout.setSpacing(0)
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10,10,10,10); content_layout.setSpacing(10)
        self.main_view_stack = QStackedWidget()
        self.scroll_area = QScrollArea(); self.scroll_area.setWidgetResizable(True); self.scroll_area.setStyleSheet("background-color: #1e1e1e;")
        self.participants_container = QWidget()
        self.grid_layout = QGridLayout(self.participants_container)
        self.grid_layout.setSpacing(10); self.grid_layout.setContentsMargins(10, 10, 10, 10)
        self.scroll_area.setWidget(self.participants_container)
        self.screen_view_label = QLabel("No one is presenting.")
        self.screen_view_label.setAlignment(Qt.AlignCenter); self.screen_view_label.setFont(QFont("Segoe UI", 20))
        self.screen_view_label.setStyleSheet("background-color: #000; border-radius: 8px;")
        self.main_view_stack.addWidget(self.scroll_area); self.main_view_stack.addWidget(self.screen_view_label)
        content_layout.addWidget(self.main_view_stack)
        controls_bar = QWidget(); controls_bar.setFixedHeight(60)
        controls_layout = QHBoxLayout(controls_bar)
        controls_layout.setContentsMargins(10, 0, 10, 0); controls_layout.setSpacing(15)

        # --- UI UPDATE: Create buttons without text ---
        self.video_btn = QPushButton(); self.audio_btn = QPushButton()
        self.screen_btn = QPushButton()
        # --- ADD HANGUP BUTTON ---
        self.hangup_btn = QPushButton()
        # ---
        self.chat_btn = QPushButton() # Chat button at the end

        # Set initial icons
        self.video_btn.setIcon(self.icon_video_off)
        self.audio_btn.setIcon(self.icon_audio_off)
        self.screen_btn.setIcon(self.icon_screen_off)
        self.hangup_btn.setIcon(self.icon_hangup) # Set hangup icon
        self.chat_btn.setIcon(self.icon_chat)

        # Set tooltips
        self.video_btn.setToolTip("Start Video")
        self.audio_btn.setToolTip("Unmute")
        self.screen_btn.setToolTip("Share Screen")
        self.hangup_btn.setToolTip("End Call") # Set hangup tooltip
        self.chat_btn.setToolTip("Show Chat")

        icon_size = QSize(22, 22)
        btn_size = 44 # Make buttons circular

        # Add hangup_btn to the list of buttons to style
        button_list = [self.video_btn, self.audio_btn, self.screen_btn, self.hangup_btn, self.chat_btn]

        for btn in button_list:
            btn.setFixedSize(btn_size, btn_size)
            btn.setIconSize(icon_size)
            btn.setCursor(Qt.PointingHandCursor)
            # Apply circular style directly here for simplicity
            btn.setStyleSheet(f"QPushButton {{ border-radius: {btn_size // 2}px; padding: 5px; border: 1px solid #5a5a5a; background-color: #3a3a3a; }} QPushButton:hover {{ background-color: #4f4f4f; }}")

        # --- END UI UPDATE ---

        # Apply initial active/inactive styles (calls update_button_style)
        self.update_button_style(self.video_btn, False); self.update_button_style(self.audio_btn, False)
        self.update_button_style(self.screen_btn, False); self.update_button_style(self.chat_btn, False)
        # Style the hangup button (it's always "active" red)
        self.hangup_btn.setStyleSheet(f"QPushButton {{ border-radius: {btn_size // 2}px; padding: 5px; border: 1px solid #b02a2f; background-color: #d93d43; }} QPushButton:hover {{ background-color: #e15258; }}")


        # --- ADD HANGUP BUTTON TO LAYOUT ---
        controls_layout.addStretch()
        controls_layout.addWidget(self.video_btn)
        controls_layout.addWidget(self.audio_btn)
        controls_layout.addWidget(self.screen_btn)
        controls_layout.addWidget(self.hangup_btn) # Add hangup button here
        controls_layout.addStretch()
        controls_layout.addWidget(self.chat_btn) # Chat button remains on the right
        # ---

        content_layout.addWidget(controls_bar)
        main_layout.addWidget(content_widget)
        self.right_panel = QWidget(); self.right_panel.setFixedWidth(350); self.right_panel.setStyleSheet("background-color: #2b2b2b;")
        right_panel_layout = QVBoxLayout(self.right_panel)
        right_panel_layout.setContentsMargins(0,0,0,0); right_panel_layout.setSpacing(0)
        tabs = QTabWidget()
        tabs.setStyleSheet("QTabBar::tab { background: #3c3c3c; color: #f0f0f0; padding: 10px; border-top-left-radius: 6px; border-top-right-radius: 6px; min-width: 80px;} QTabBar::tab:selected { background: #4f4f4f; } QTabWidget::pane { border: none; background-color: #2b2b2b; }")
        tabs.addTab(self._create_chat_widget(), "Chat"); tabs.addTab(self._create_file_widget(), "Files")
        right_panel_layout.addWidget(tabs)
        main_layout.addWidget(self.right_panel)
        self.right_panel.hide()
        self.video_btn.clicked.connect(self.toggle_camera); self.audio_btn.clicked.connect(self.toggle_audio)
        self.screen_btn.clicked.connect(self.toggle_screen_share)
        # --- CONNECT HANGUP BUTTON ---
        self.hangup_btn.clicked.connect(self.hangup_and_close)
        # ---
        self.chat_btn.clicked.connect(self.toggle_right_panel)

    def _create_chat_widget(self):
        # ... (unchanged) ...
        widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(10,10,10,10)
        self.chat_display = QTextEdit(); self.chat_display.setReadOnly(True); self.chat_display.setStyleSheet("background-color: #3c3c3c; border-radius: 6px; padding: 5px; color: #f0f0f0;")
        self.message_input = QLineEdit(); self.message_input.setPlaceholderText("Type a message..."); self.message_input.setStyleSheet("background-color: #3c3c3c; border-radius: 6px; padding: 5px; color: #f0f0f0;")
        self.message_input.returnPressed.connect(self.send_message)
        send_btn = QPushButton("Send"); send_btn.setStyleSheet("background-color: #5a5a5a; color: white; border-radius: 6px; padding: 5px 10px;"); send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.clicked.connect(self.send_message)
        input_layout = QHBoxLayout(); input_layout.addWidget(self.message_input); input_layout.addWidget(send_btn)
        layout.addWidget(self.chat_display); layout.addLayout(input_layout)
        return widget

    def _create_file_widget(self):
        # ... (unchanged) ...
        widget = QWidget(); layout = QVBoxLayout(widget); layout.setContentsMargins(10,10,10,10)
        upload_btn = QPushButton("Upload File"); upload_btn.setStyleSheet("background-color: #5a5a5a; color: white; border-radius: 6px; padding: 5px 10px;"); upload_btn.setCursor(Qt.PointingHandCursor)
        upload_btn.clicked.connect(self.upload_file)
        self.progress_bar = QProgressBar(); self.progress_bar.setVisible(False); self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("QProgressBar { border: 1px solid #5a5a5a; border-radius: 5px; background-color: #3c3c3c; text-align: center; color: white;} QProgressBar::chunk { background-color: #3aa76d; border-radius: 5px;}")
        self.file_list = QListWidget(); self.file_list.itemDoubleClicked.connect(self.download_file); self.file_list.setStyleSheet("background-color: #3c3c3c; border-radius: 6px; padding: 5px; color: #f0f0f0;")
        layout.addWidget(upload_btn); layout.addWidget(self.progress_bar); layout.addWidget(QLabel("Available Files (Double-click to download):")); layout.addWidget(self.file_list)
        return widget
    # ---------------------------------------------------------------------

    def connect_to_servers(self):
        # ... (unchanged - uses the provided correct logic) ...
        self.statusBar().showMessage('Connecting...')
        all_connected = True
        self.client_connections = {}

        client_configs = [
            ('video', VideoClient, [], {}),
            ('audio', AudioClient, [], {}),
            ('chat', ChatClient, [self.username], {}),
            ('file', FileClient, [self.username], {}),
            ('screen', ScreenClient, [lambda s: self.presenter_update_signal.emit(s)], {})
        ]

        for name, client_class, extra_init_args, connect_kwargs in client_configs:
            try:
                instance_args = (self.client_id, self.server_ip) + tuple(extra_init_args)
                print(f"[GUI DEBUG] Initializing {name} with args: {instance_args}")
                client = client_class(*instance_args)

                if name == 'chat' and client:
                    client.register_callback(lambda msg: self.new_message_signal.emit(msg))
                    client.register_disconnect_callback(lambda: self.server_disconnected_signal.emit())

                if client.connect(**connect_kwargs):
                    self.client_connections[name] = client
                    print(f"[GUI] Connected to {name.capitalize()} Server.")
                    if name == 'audio' and client: client.start_speakers()
                    if name == 'file' and client:
                        client.register_new_file_callback(lambda n: self.new_file_signal.emit(n))
                        client.register_progress_callback(lambda o, f, p, c, t: self.progress_signal.emit(o, f, p, c, t))
                else:
                    QMessageBox.warning(self, 'Connection Error', f'{name.capitalize()} Server connection failed.')
                    all_connected = False; break
            except Exception as e:
                print(f"[GUI ERROR] Failed to initialize/connect {name}: {e}\n{traceback.format_exc()}")
                QMessageBox.critical(self, 'Initialization Error', f'Failed during {name} setup: {e}')
                all_connected = False; break

        if all_connected:
            self.statusBar().showMessage('All services connected!', 3000)
            self.video_client = self.client_connections.get('video')
            self.audio_client = self.client_connections.get('audio')
            self.chat_client = self.client_connections.get('chat')
            self.file_client = self.client_connections.get('file')
            self.screen_client = self.client_connections.get('screen')
        else:
            self.statusBar().showMessage('Connection failed. Closing...', 5000)
            self.running = False
            QTimer.singleShot(50, lambda: self.closeEvent(None, skip_dialog=True))
        return all_connected

    # --- UI UPDATE: Stylesheet function updated for circular buttons ---
    def update_button_style(self, button, is_active):
        btn_size = 44
        border_radius = btn_size // 2

        base_style = f"""
            QPushButton {{
                border-radius: {border_radius}px;
                padding: 5px;
                border: 1px solid #5a5a5a; /* Default border */
            }}
            QPushButton:hover {{
                background-color: #4f4f4f; /* Darker grey on hover */
            }}
            QPushButton:disabled {{
                 background-color: #2a2a2a;
                 border: 1px solid #404040;
            }}
        """

        active_bg = "#d93d43"      # Red background when active (for Video/Audio/Screen)
        inactive_bg = "#3a3a3a"    # Dark grey background when inactive
        active_border = "#b02a2f"  # Slightly darker red border when active

        # --- Don't style hangup button here, it has its own style set in init_ui ---
        if button == self.hangup_btn:
            return # Hangup button style is static red

        # Specific styling for the chat button
        elif button == self.chat_btn:
            chat_active_color = "#007bff" # Blue color for active state indication
            if is_active:
                button.setIcon(self.icon_chat_active) # Icon color changes
                # Keep background standard inactive, but add blue border
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {inactive_bg}; border: 1px solid {chat_active_color}; }}")
            else:
                button.setIcon(self.icon_chat) # Standard icon color
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {inactive_bg}; }}")
        # Styling for Video, Audio, Screen buttons
        else:
            if is_active:
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {active_bg}; border: 1px solid {active_border}; }}")
            else:
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {inactive_bg}; }}")
    # --- END UI UPDATE ---

    def toggle_right_panel(self):
        # ... (unchanged) ...
        is_visible = not self.right_panel.isVisible()
        self.right_panel.setVisible(is_visible)
        self.update_button_style(self.chat_btn, is_visible)
        self.chat_btn.setToolTip("Hide Chat" if is_visible else "Show Chat")
        if is_visible and self.file_client: self.refresh_files()

    def add_or_update_participant(self, client_id, username):
        # ... (unchanged) ...
        self.clients[client_id] = {'username': username}
        tile_exists = client_id in self.participant_tiles
        needs_redraw = False
        if not tile_exists:
            tile = ParticipantTile(username=username, client_id=client_id)
            self.participant_tiles[client_id] = tile
            needs_redraw = True
        elif self.participant_tiles[client_id].username != username:
            self.participant_tiles[client_id].username = username
            self.participant_tiles[client_id].update()
        return needs_redraw

    def remove_participant(self, client_id):
       # ... (unchanged) ...
        tile_removed = False
        if client_id in self.participant_tiles:
            tile_to_remove = self.participant_tiles.pop(client_id)
            if tile_to_remove: tile_to_remove.deleteLater()
            tile_removed = True
        if client_id in self.clients:
            del self.clients[client_id]
        return tile_removed

    def redraw_participant_grid(self):
       # ... (unchanged) ...
        if threading.current_thread() != threading.main_thread():
            QTimer.singleShot(0, self.redraw_participant_grid)
            return
        print("[GUI DEBUG] Redrawing participant grid...")
        while (item := self.grid_layout.takeAt(0)) is not None:
            widget = item.widget()
            if widget: widget.setParent(None)
        tiles = list(self.participant_tiles.values())
        n = len(tiles)
        if n == 0:
            print("[GUI DEBUG] Grid empty.")
            self.participants_container.updateGeometry()
            return
        cols = 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4
        print(f"[GUI DEBUG] Grid params: n={n}, cols={cols}")
        for i, tile in enumerate(tiles):
            self.grid_layout.addWidget(tile, i // cols, i % cols)
        self.participants_container.updateGeometry()
        self.scroll_area.updateGeometry()
        print("[GUI DEBUG] Grid redraw complete.")


    # --- UI UPDATE: Update icon and tooltip ---
    def toggle_camera(self):
        if not self.video_client: return
        if self.video_client.sending:
            self.video_client.stop_camera()
            self.video_btn.setIcon(self.icon_video_off)
            self.video_btn.setToolTip("Start Video")
            self.update_button_style(self.video_btn, False)
        elif self.video_client.start_camera():
            self.video_btn.setIcon(self.icon_video_on)
            self.video_btn.setToolTip("Stop Video")
            self.update_button_style(self.video_btn, True)

    # --- UI UPDATE: Update icon and tooltip ---
    def toggle_audio(self):
        if not self.audio_client: return
        if self.audio_client.sending:
            self.audio_client.stop_microphone()
            self.audio_btn.setIcon(self.icon_audio_off)
            self.audio_btn.setToolTip("Unmute")
            self.update_button_style(self.audio_btn, False)
        elif self.audio_client.start_microphone():
            self.audio_btn.setIcon(self.icon_audio_on)
            self.audio_btn.setToolTip("Mute")
            self.update_button_style(self.audio_btn, True)

    def toggle_screen_share(self):
        # Keeps the stable logic from the user-provided code
        if not self.screen_client: return
        if self.screen_client.is_presenting:
            self.screen_client.stop_sharing()
        else:
            self.screen_client.start_sharing()

    def update_video_frames(self):
       # ... (unchanged) ...
        if not self.video_client or not self.running: return
        local_tile = self.participant_tiles.get(self.client_id)
        if local_tile:
            if self.video_client.sending and self.video_client.cap and self.video_client.cap.isOpened():
                ret, frame = self.video_client.cap.read()
                if ret: local_tile.update_frame(cv2.flip(frame, 1))
                else: local_tile.clear_frame()
            else: local_tile.clear_frame()
        try:
            remote_frames = self.video_client.get_frames()
        except Exception as e:
            remote_frames = {}
        active_remote_cids = set(remote_frames.keys())
        cids_processed_this_cycle = {self.client_id}
        for cid, frame in remote_frames.items():
            if cid == self.client_id: continue
            tile = self.participant_tiles.get(cid)
            if tile:
                tile.update_frame(frame)
                cids_processed_this_cycle.add(cid)
        for cid, tile in list(self.participant_tiles.items()):
            if cid not in cids_processed_this_cycle:
                tile.clear_frame()

    # --- UI UPDATE + Keep stable logic ---
    def on_presenter_update(self, status):
        """Handles screen share status and UI stack/icon switching."""
        if not self.running: return
        presenter_id = status.get("presenter_id")
        self.screen_btn.setEnabled(True) # Re-enable button by default

        if presenter_id is None:
            # No one is presenting
            self.screen_btn.setIcon(self.icon_screen_off)
            self.screen_btn.setToolTip("Share Screen")
            self.update_button_style(self.screen_btn, False)

            self.screen_view_label.clear(); self.screen_view_label.setText("No one is presenting.")
            # Switch back to the video grid if currently showing the screen share label
            if self.main_view_stack.currentWidget() == self.screen_view_label:
                self.main_view_stack.setCurrentWidget(self.scroll_area)

        elif presenter_id == self.client_id:
            # We are presenting
            self.screen_btn.setIcon(self.icon_screen_on)
            self.screen_btn.setToolTip("Stop Sharing")
            self.update_button_style(self.screen_btn, True)

            # Switch to the "You are presenting" label if not already there
            if self.main_view_stack.currentWidget() != self.screen_view_label:
                self.main_view_stack.setCurrentWidget(self.screen_view_label)
            self.screen_view_label.setText("You are presenting...") # Update label text

        else:
            # Someone else is presenting
            presenter_name = self.clients.get(presenter_id, {}).get('username', f"User {presenter_id}")

            self.screen_btn.setIcon(self.icon_screen_off) # Show inactive icon
            self.screen_btn.setToolTip(f"{presenter_name} is presenting")
            self.update_button_style(self.screen_btn, False) # Use inactive style
            self.screen_btn.setEnabled(False) # Disable the button since someone else is sharing

            # Switch to the screen share view label if not already there
            if self.main_view_stack.currentWidget() != self.screen_view_label:
                self.main_view_stack.setCurrentWidget(self.screen_view_label)

            # Update label text to show who is presenting
            current_text = self.screen_view_label.text(); new_text = f"{presenter_name} is presenting..."
            if current_text != new_text: self.screen_view_label.setText(new_text)
    # --- END UI UPDATE ---

    def update_screen_frame(self):
        # ... (unchanged) ...
        if not self.screen_client or self.screen_client.is_presenting or not self.running: return
        frame_data = self.screen_client.get_frame()
        if frame_data == 'EMPTY': return
        elif frame_data is None: return
        else:
            pixmap = QPixmap(); pixmap.loadFromData(frame_data, "JPEG")
            if not pixmap.isNull(): self.screen_view_label.setPixmap(pixmap.scaled(self.screen_view_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def on_new_message(self, msg):
        # ... (unchanged - uses the provided correct logic) ...
        if not self.running: return
        msg_type = msg.get('type')
        if msg_type == 'user_list':
            current_cids_in_list = set()
            users = msg.get('users', [])
            redraw_needed = False
            for user in users:
                user_id = user.get('client_id')
                user_name = user.get('username')
                if user_id is None: continue
                current_cids_in_list.add(user_id)
                local_name = (
                    f"{user_name} (You)" if user_id == self.client_id else user_name
                )
                if self.add_or_update_participant(user_id, local_name):
                    redraw_needed = True
            ids_to_remove = set(self.participant_tiles.keys()) - current_cids_in_list
            for cid in ids_to_remove:
                if self.remove_participant(cid):
                    redraw_needed = True
            if redraw_needed or not self.participant_tiles or set(self.participant_tiles.keys()) == {self.client_id}:
                QTimer.singleShot(0, self.redraw_participant_grid)
        elif msg_type == 'message':
            self.chat_display.append(f"<b>{msg.get('username', '???')}:</b> {msg.get('text', '')}")
        elif msg_type == 'system':
            self.chat_display.append(f"<i style='color: #aaaaaa;'>{msg.get('text', '')}</i>")
        self.chat_display.verticalScrollBar().setValue(self.chat_display.verticalScrollBar().maximum())

    def _trigger_file_list_ui_update(self):
        """
        Reads the *current* local file list from the client
        and schedules a UI update. Does NOT fetch from server.
        This is fast and should be used after a notification.
        """
        if not self.file_client or not self.running: return
        
        try:
            # This is safe to call from any thread, get_available_files is locked
            files = self.file_client.get_available_files() or []
            print(f"[GUI DEBUG] Triggering UI update with {len(files)} local files.")
            QTimer.singleShot(0, lambda: self._update_file_list_ui(files))
        except Exception as e:
            print(f"[GUI ERROR] Failed to _trigger_file_list_ui_update: {e}")
    def on_new_file(self, notification):
        # ... (unchanged) ...
        if not self.running: return
        self._trigger_file_list_ui_update(); self.statusBar().showMessage(f"New file: {notification.get('filename','')}", 3000)
    def on_transfer_progress(self, op, filename, progress, current, total):
       # ... (unchanged) ...
        if not self.running: return
        self.progress_bar.setVisible(True); self.progress_bar.setValue(int(progress))
        self.progress_bar.setFormat(f"{op.capitalize()}: {progress:.0f}%")
        if progress >= 100:
            QTimer.singleShot(2500, lambda: self.progress_bar.setVisible(False))
            if op == 'download': QMessageBox.information(self, "Download Complete", f"'{filename}' saved to Downloads")
            elif op == 'upload': 
                print("[GUI] Upload complete. Waiting for server notification.")

    def send_message(self):
        # ... (unchanged) ...
        text_to_send = self.message_input.text().strip()
        if self.chat_client and text_to_send:
            self.chat_client.send_message(text_to_send)
            self.message_input.clear()
    def upload_file(self):
        # ... (unchanged - but likely needs debugging in file_client.py) ...
        if not self.file_client: return
        filepath, _ = QFileDialog.getOpenFileName(self, "Select File to Upload")
        if filepath:
            self.progress_bar.setVisible(True); self.progress_bar.setValue(0)
            threading.Thread(target=self.file_client.upload_file, args=(filepath,), daemon=True).start()
    def download_file(self, item):
       # ... (unchanged) ...
        if not self.file_client: return
        file_id = item.data(Qt.UserRole)
        if file_id:
            save_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
            os.makedirs(save_dir, exist_ok=True)
            try: filename = item.text().split(' (')[0]
            except: filename = f"downloaded_file_{file_id}"
            save_path = os.path.join(save_dir, filename)
            self.progress_bar.setVisible(True); self.progress_bar.setValue(0)
            threading.Thread(target=self.file_client.download_file, args=(file_id, save_path), daemon=True).start()
    def refresh_files(self):
       # ... (unchanged) ...
        if not self.file_client or not self.running: return
        threading.Thread(target=self._do_refresh_files, daemon=True).start()
    def _do_refresh_files(self):
       # ... (unchanged) ...
        print("[GUI DEBUG] Refreshing file list...")
        try:
            print("[GUI DEBUG] Calling client.refresh_file_list()...")
            refresh_success = self.file_client.refresh_file_list() 
            
            if not refresh_success:
                 print("[GUI WARN] client.refresh_file_list() returned False.")
                 return 
            
            print("[GUI DEBUG] Refresh complete. Now getting local list...")
            # 2. Get the new list that we just refreshed.
            files = self.file_client.get_available_files() or []
            QTimer.singleShot(0, lambda: self._update_file_list_ui(files))
        except Exception as e: 
            print(f"[GUI ERROR] Failed to get available files: {e}")
            import traceback
            traceback.print_exc()
    def _update_file_list_ui(self, files):
        # ... (unchanged) ...
        if not self.running: return
        self.file_list.clear()
        for file_info in files:
            uploader = file_info.get('uploader', 'Unknown'); size_str = f"{file_info.get('filesize', 0)}B"
            item = QListWidgetItem(f"{file_info.get('filename', 'Unnamed File')} ({size_str}) by {uploader}")
            item.setData(Qt.UserRole, file_info.get('file_id')); self.file_list.addItem(item)
        print(f"[GUI DEBUG] File list UI updated with {len(files)} files.")
    # ---------------------------------------------------------------------

    def closeEvent(self, event, skip_dialog=False):
       # ... (unchanged) ...
        if self._disconnecting:
            if event: event.ignore(); return
        if not skip_dialog and not self._skip_close_dialog:
            reply = QMessageBox.question(self, 'Exit', 'Are you sure?', QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                if event: event.ignore(); return
        print("Initiating GUI shutdown...")
        self._disconnecting = True
        self.running = False
        if hasattr(self, 'video_timer') and self.video_timer.isActive(): self.video_timer.stop()
        if hasattr(self, 'screen_timer') and self.screen_timer.isActive(): self.screen_timer.stop()
        print("Timers stopped.")
        self.statusBar().showMessage('Disconnecting...')
        QApplication.processEvents()
        clients_to_disconnect = ['screen', 'file', 'chat', 'audio', 'video']
        threads = []
        for name in clients_to_disconnect:
            client = self.client_connections.get(name)
            if client:
                print(f"Signaling {name} client to disconnect...")
                t = threading.Thread(target=client.disconnect, name=f"{name}_disconnect")
                t.start(); threads.append((name, t))
        start_wait = time.time(); max_wait = 1.5
        for name, t in threads:
            remaining = max_wait - (time.time() - start_wait)
            if remaining <= 0: break
            t.join(timeout=remaining)
            if t.is_alive(): print(f"[GUI WARN] Disconnect thread for {name} timed out.")
        print("All clients signaled to disconnect.")
        if event: event.accept()
        print("GUI shutdown sequence complete.")


if __name__ == '__main__':
    # ... (unchanged) ...
    app = QApplication(sys.argv)
    window = None
    try:
        window = LANCommClient()
        if window.running:
            window.showMaximized()
            sys.exit(app.exec_())
        else:
            print("Application failed to initialize properly. Exiting.")
            if window: window.close()
            sys.exit(1)
    except Exception as e:
        print(f"Unhandled exception during startup: {e}\n{traceback.format_exc()}")
        if window and hasattr(window, 'closeEvent'):
            window.closeEvent(None, skip_dialog=True)
        sys.exit(1)