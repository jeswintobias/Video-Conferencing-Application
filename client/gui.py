"""
Main GUI Application - [REWRITE]
- [FIXED] Corrects a layout bug in ChatMessageWidget that made system messages invisible.
- Implements a unified chat/file widget list.
- Implements an unread message badge on the chat icon.
- Fixes participant synchronization issues.
- Reliably clears frozen video frames.
- Robust error handling and state management.
- Stable shutdown sequence.
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
                             QTabWidget, QStackedWidget, QSizePolicy, QStyle, QFrame, QComboBox) # Added QFrame
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot, QSize, QRect, QObject, QRunnable, QThreadPool
from PyQt5.QtGui import (QImage, QPixmap, QFont, QPainter, QColor, QLinearGradient,
                             QIcon)
from PyQt5.QtWidgets import QDialog, QFormLayout

import qtawesome 
try:
    # --- IMPORTANT ---
    from video_client import VideoClient
    from audio_client import AudioClient
    from chat_client import ChatClient
    from file_client import FileClient # <-- MUST BE THE NEW VERSION
    from screen_client import ScreenClient
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils.config import *
except ImportError as e:
    print(f"FATAL ERROR: Could not import client modules or config: {e}")
    print("Ensure all .py files are present and utils/config.py exists.")
    sys.exit(1)


class ParticipantTile(QWidget):
    """Final robust ParticipantTile widget."""
    doubleClicked = pyqtSignal(int)
    def __init__(self, username="", client_id=None, parent=None):
        super().__init__(parent)
        self.username = username
        self.client_id = client_id
        self.setMinimumSize(200, 112) # 16:9 base
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pixmap = None
        self._has_frame = False
        self.current_reaction = None  # Store current reaction emoji
        self.reaction_timer = None  # Timer to clear reaction after 5 seconds


    def update_frame(self, frame_bgr):
        try:
            h, w, _ = frame_bgr.shape
            if h <= 0 or w <= 0: self.clear_frame(); return
            rgb_image = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            qt_image = QImage(rgb_image.data, w, h, w * 3, QImage.Format_RGB888)
            new_pixmap = QPixmap.fromImage(qt_image)
            if not new_pixmap.isNull():
                if not self._has_frame or self._pixmap is None or new_pixmap.cacheKey() != self._pixmap.cacheKey():
                    self._pixmap = new_pixmap
                    self._has_frame = True
                    self.update() # Request repaint only if changed
            else: 
                if self._has_frame: self._has_frame = False; self.update()
        except Exception as e:
            if self._has_frame: self._has_frame = False; self.update()

    def clear_frame(self):
        if self._has_frame:
            self._has_frame = False
            self._pixmap = None
            self.update() # Request repaint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor("#1f1f1f"))
        gradient.setColorAt(1, QColor("#2a2a2a"))
        painter.fillRect(self.rect(), gradient)
        target_rect = self.rect().adjusted(2, 2, -2, -2)

        if self._has_frame and self._pixmap and not self._pixmap.isNull():
            pixmap_scaled = self._pixmap.scaled(target_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = target_rect.left() + (target_rect.width() - pixmap_scaled.width()) // 2
            y = target_rect.top() + (target_rect.height() - pixmap_scaled.height()) // 2
            painter.drawPixmap(x, y, pixmap_scaled)
        else: # Draw initials placeholder
            painter.setPen(QColor("#8a8d91"))
            font_size = min(90, max(10, int(self.height() * 0.2))) # Cap at 90pt
            font = QFont("Segoe UI", font_size, QFont.Bold)
            painter.setFont(font)
            initials = self._make_initials(self.username)
            painter.drawText(self.rect(), Qt.AlignCenter, initials)
        
        name_bar_height = min(60, max(20, int(self.height() * 0.15))) # Cap bar height
        painter.fillRect(0, self.height() - name_bar_height, self.width(), name_bar_height, QColor(0, 0, 0, 150))
        painter.setPen(Qt.white)
        font_size = min(20, max(8, int(name_bar_height * 0.5))) # Cap at 20pt
        font = QFont("Segoe UI", font_size)
        painter.setFont(font)
        display_name = self.username if self.username else f"User {self.client_id}"
        metrics = painter.fontMetrics()
        elided_text = metrics.elidedText(display_name, Qt.ElideRight, self.width() - 10)
        painter.drawText(QRect(5, self.height() - name_bar_height, self.width() - 10, name_bar_height), Qt.AlignVCenter | Qt.AlignLeft, elided_text)
        if self.current_reaction:
            reaction_font = QFont("Segoe UI Emoji", 26) # <-- Changed from 28 to 26
            painter.setFont(reaction_font)
            
            reaction_size = 44 # Keep the 44px circle
            badge_padding = 8  # Keep the 8px padding
            
            reaction_rect = QRect(
                self.width() - reaction_size - badge_padding, # X: Right edge - size - padding
                badge_padding,                               # Y: Top edge + padding
                reaction_size, 
                reaction_size
            )
            painter.setBrush(QColor(0, 0, 0, 180))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(reaction_rect) 
            painter.setPen(Qt.transparent) 
            painter.drawText(reaction_rect, Qt.AlignCenter, self.current_reaction)
        
        painter.end()

    def _make_initials(self, name):
        if not name: return "?"
        parts = name.replace("(You)", "").strip().split()
        if not parts: return "?"
        if len(parts) == 1: return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()
    
    def mouseDoubleClickEvent(self, event):        
        """Emits the doubleClicked signal when the tile is double-clicked."""
        self.doubleClicked.emit(self.client_id)
        event.accept()
    
    def show_reaction(self, emoji):
        """Show a reaction emoji above the tile for 5 seconds."""
        self.current_reaction = emoji
        self.update()  # Trigger repaint
        
        # Clear previous timer if exists
        if self.reaction_timer:
            self.reaction_timer.stop()
        
        # Create timer to clear reaction after 5 seconds
        self.reaction_timer = QTimer()
        self.reaction_timer.setSingleShot(True)
        self.reaction_timer.timeout.connect(self.clear_reaction)
        self.reaction_timer.start(5000)  # 5 seconds
    
    def clear_reaction(self):
        """Clear the current reaction."""
        self.current_reaction = None
        self.update()  # Trigger repaint
        if self.reaction_timer:
            self.reaction_timer.stop()
            self.reaction_timer = None


class ScreenShareTile(QWidget):
    """Special tile that displays screen share."""
    doubleClicked = pyqtSignal()  # Signal for pinning
    
    def __init__(self, presenter_name="", presenter_id=None, parent=None):
        super().__init__(parent)
        self.presenter_name = presenter_name
        self.presenter_id = presenter_id
        self.setMinimumSize(400, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    
        self.container = QWidget(self)
        self.container.setStyleSheet("background-color: #1e1e1e; border-radius: 8px;")
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.container)
        
        self.screen_label = QLabel("Screen Share", self.container)
        self.screen_label.setAlignment(Qt.AlignCenter)
        self.screen_label.setStyleSheet("background-color: #000; border-radius: 8px;")
        self.name_bar = QWidget(self.container)
        self.name_bar.setStyleSheet("background-color: rgba(0, 0, 0, 180); border-radius: 4px;")
        name_layout = QHBoxLayout(self.name_bar)
        name_layout.setContentsMargins(10, 5, 10, 5)
        name_text = QLabel(f"{presenter_name} - Screen Share")
        name_text.setStyleSheet("color: white; font-weight: bold;")
        name_layout.addWidget(name_text)
        name_layout.addStretch()
        # Pin indicator (dynamic)
        self.pin_indicator = QLabel("(Double-click to unpin)")
        self.pin_indicator.setStyleSheet("color: #888; font-size: 9pt;")
        name_layout.addWidget(self.pin_indicator)
    
    def resizeEvent(self, event):
        """Position screen label when widget is resized."""
        super().resizeEvent(event)
        # Container should fill the widget (accounting for margins)
        margins = 5
        container_x = margins
        container_y = margins
        container_width = self.width() - (2 * margins)
        container_height = self.height() - (2 * margins)
        self.container.setGeometry(container_x, container_y, container_width, container_height)
        
        container_rect = self.container.rect()
        
        # Screen label fills the container
        self.screen_label.setGeometry(container_rect)
        
        # Name bar at bottom
        name_bar_height = 30
        self.name_bar.setGeometry(0, container_rect.height() - name_bar_height, 
                                 container_rect.width(), name_bar_height)
    
    def mouseDoubleClickEvent(self, event):
        """Emits the doubleClicked signal when the tile is double-clicked."""
        self.doubleClicked.emit()
        event.accept()
    
    def update_pin_status(self, is_pinned):
        """Update the pin indicator text based on pinned state."""
        if is_pinned:
            self.pin_indicator.setText("(Double-click to unpin)")
        else:
            self.pin_indicator.setText("(Double-click to pin)")
    
    def update_screen_frame(self, frame_data):
        """Update the screen share display with JPEG frame data."""
        if frame_data == 'EMPTY' or frame_data is None:
            return
        try:
            pixmap = QPixmap()
            pixmap.loadFromData(frame_data, "JPEG")
            if not pixmap.isNull():
                self.screen_label.setPixmap(
                    pixmap.scaled(
                        self.screen_label.size(), 
                        Qt.KeepAspectRatio, 
                        Qt.SmoothTransformation
                    )
                )
        except Exception as e:
            print(f"[ScreenShareTile ERROR] Failed to update screen: {e}")
    
    def clear_all(self):
        """Clear screen display."""
        self.screen_label.clear()
        self.screen_label.setText("Screen Share")

class ChatMessageWidget(QWidget):
    """
    A custom widget for displaying a single chat message bubble.
    [FIXED] This version correctly uses layouts for all message types.
    """
    def __init__(self, username, message_text, is_self=False, is_system=False, is_private=False, recipient_name=None, sender_name=None):
        super().__init__()
        
        # Create the main layout for this widget
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 2, 0, 2)
        
        # System Message Style
        if is_system:
            self.bubble = QFrame()
            self.bubble.setStyleSheet("background-color: transparent; border: none;")
            
            bubble_layout = QVBoxLayout(self.bubble)
            bubble_layout.setContentsMargins(8, 6, 8, 6)

            msg_label = QLabel(f"<i>{message_text}</i>")
            msg_label.setStyleSheet("color: #a0a0a0; background: transparent;")
            msg_label.setWordWrap(True)
            msg_label.setAlignment(Qt.AlignCenter) # Center the text
            
            bubble_layout.addWidget(msg_label)
            main_layout.addWidget(self.bubble, 0, Qt.AlignHCenter) # Center the bubble
        
        # User Message Style
        else:
            self.bubble = QFrame()
            bubble_layout = QVBoxLayout(self.bubble)
            bubble_layout.setContentsMargins(8, 6, 8, 6)
            
            # Username row
            user_row = QHBoxLayout()
            user_row.setContentsMargins(0, 0, 0, 0)
            user_row.setSpacing(5)
            
            user_label = QLabel(username)
            user_label.setStyleSheet("font-weight: bold; color: #e0e0e0; background: transparent;")
            user_row.addWidget(user_label)
            user_row.addStretch()
            bubble_layout.addLayout(user_row)
            
            msg_label = QLabel(message_text)
            msg_label.setStyleSheet("color: #f0f0f0; background: transparent;")
            msg_label.setWordWrap(True)
            
            bubble_layout.addWidget(msg_label)
            
            if is_self:
                self.bubble.setStyleSheet("background-color: #005c9e; border-radius: 8px;")
                main_layout.addStretch()
                main_layout.addWidget(self.bubble)
            else:
                self.bubble.setStyleSheet("background-color: #3a3a3a; border-radius: 8px;")
                main_layout.addWidget(self.bubble)
                main_layout.addStretch()

# --- CHAT FILE WIDGET ---
class ChatFileWidget(QWidget):
    """
    A custom widget for displaying a single file bubble.
    """
    # Signal: file_id
    download_requested = pyqtSignal(str)
    
    def __init__(self, username, file_info, is_self=False):
        super().__init__()
        
        self.file_id = file_info.get('file_id')
        self.filename = file_info.get('filename', 'Unnamed File')
        filesize = file_info.get('filesize', 0)

        # Format filesize
        if filesize < 1024: size_str = f"{filesize} B"
        elif filesize < 1024**2: size_str = f"{filesize/1024:.1f} KB"
        else: size_str = f"{filesize/(1024**2):.1f} MB"
        
        # --- Bubble ---
        self.bubble = QFrame()
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(8, 6, 8, 6)
        bubble_layout.setSpacing(6)

        user_label = QLabel(username)
        user_label.setStyleSheet("font-weight: bold; color: #e0e0e0; background: transparent;")
        bubble_layout.addWidget(user_label)
        
        # --- File Info Row ---
        file_row = QWidget()
        file_row_layout = QHBoxLayout(file_row)
        file_row_layout.setContentsMargins(0,0,0,0)
        file_row_layout.setSpacing(8)
        
        icon_label = QLabel()
        icon_label.setPixmap(qtawesome.icon('fa5s.file-alt', color='#d0d0d0').pixmap(QSize(32, 32)))
        file_row_layout.addWidget(icon_label)
        
        details_layout = QVBoxLayout()
        details_layout.setSpacing(0)
        filename_label = QLabel(self.filename)
        filename_label.setStyleSheet("color: #f0f0f0; background: transparent;")
        filename_label.setWordWrap(True)
        filesize_label = QLabel(size_str)
        filesize_label.setStyleSheet("color: #a0a0a0; background: transparent; font-size: 9pt;")
        details_layout.addWidget(filename_label)
        details_layout.addWidget(filesize_label)
        file_row_layout.addLayout(details_layout)
        file_row_layout.addStretch()
        bubble_layout.addWidget(file_row)

        # --- Download Button ---
        self.download_btn = QPushButton(" Download")
        self.download_btn.setIcon(qtawesome.icon('fa5s.download', color='white'))
        self.download_btn.setCursor(Qt.PointingHandCursor)
        self.download_btn.setStyleSheet("""
            QPushButton { 
                background-color: #5a5a5a; color: white; 
                border: none; border-radius: 4px; padding: 6px; 
            }
            QPushButton:hover { background-color: #6a6a6a; }
        """)
        self.download_btn.clicked.connect(self.on_download_clicked)
        bubble_layout.addWidget(self.download_btn)

        # --- Main Layout (Alignment) ---
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        if is_self:
            self.bubble.setStyleSheet("background-color: #004c7e; border-radius: 8px;") # Darker blue for files
            main_layout.addStretch()
            main_layout.addWidget(self.bubble)
        else:
            self.bubble.setStyleSheet("background-color: #3a3a3a; border-radius: 8px;")
            main_layout.addWidget(self.bubble)
            main_layout.addStretch()

        self.setContentsMargins(0, 2, 0, 2)

    def on_download_clicked(self):
        self.download_requested.emit(self.file_id)
        self.download_btn.setText(" Downloading...")
        self.download_btn.setEnabled(False)

# --- UPLOAD WORKER CLASS ---
class UploadWorker(QRunnable):
    """
    Worker thread for file uploads.
    Emits a signal with the result (file_info dict or None).
    """
    class WorkerSignals(QObject):
        finished = pyqtSignal(object) # object can be dict or None

    def __init__(self, file_client, filepath):
        super().__init__()
        self.file_client = file_client
        self.filepath = filepath
        self.signals = self.WorkerSignals()

    @pyqtSlot()
    def run(self):
        """Runs the upload and emits the result"""
        result = self.file_client.upload_file(self.filepath)
        self.signals.finished.emit(result)

class ClickableLabel(QLabel):
    """A QLabel that emits a doubleClicked signal."""
    doubleClicked = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def mouseDoubleClickEvent(self, event):
        """Emits the signal on a double-click."""
        self.doubleClicked.emit()
        event.accept()
class LoginDialog(QDialog):
    """
    A custom dialog to get username and server IP, matching the main GUI's dark theme.
    """
    def __init__(self, parent=None, default_ip='127.0.0.1'):
        super().__init__(parent)
        self.username = ""
        self.server_ip = ""
        self.setWindowTitle("Connect to LAN Comm")
        self.setModal(True)
        # Remove the '?' help button on Windows/Linux
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint) 

        # --- Apply Dark Theme ---
        self.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                color: #f0f0f0;
                border: 1px solid #5a5a5a;
            }
            QLabel {
                font-size: 10pt;
                color: #a0a0a0;
                padding-top: 5px; /* Aligns label with the center of the input box */
            }
            QLineEdit {
                background-color: #3c3c3c;
                border-radius: 6px;
                padding: 8px;
                color: #f0f0f0;
                border: 1px solid #5a5a5a;
                font-size: 10pt;
            }
            QLineEdit:focus {
                border: 1px solid #007bff; /* Highlight on focus */
            }
        """)

        # --- Layouts ---
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Connect to Server")
        title.setStyleSheet("font-size: 16pt; font-weight: bold; padding-bottom: 10px; color: white;")
        title.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title)
        
        # Form Layout for inputs
        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("Enter your name")
        self.server_ip_input = QLineEdit()
        self.server_ip_input.setText(default_ip)
        self.server_ip_input.setPlaceholderText("Enter server IP")

        form_layout.addRow(QLabel("Username:"), self.username_input)
        form_layout.addRow(QLabel("Server IP:"), self.server_ip_input)
        main_layout.addLayout(form_layout)

        # Button Layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addStretch()

        self.cancel_btn = QPushButton("Exit")
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #5a5a5a; color: white;
                border: none; border-radius: 6px; padding: 8px 16px;
                font-size: 10pt;
            }
            QPushButton:hover { background-color: #6a6a6a; }
        """)
        self.cancel_btn.clicked.connect(self.reject) # QDialog's built-in reject slot
        
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setDefault(True) # Pressing Enter will click this
        self.connect_btn.setStyleSheet("""
            QPushButton {
                background-color: #007bff; color: white;
                border: none; border-radius: 6px; padding: 8px 16px;
                font-size: 10pt; font-weight: bold;
            }
            QPushButton:hover { background-color: #008cff; }
        """)
        self.connect_btn.clicked.connect(self.accept_data) # Use custom accept method

        button_layout.addWidget(self.cancel_btn)
        button_layout.addWidget(self.connect_btn)
        main_layout.addLayout(button_layout)
        
        # --- Connect signals for Enter key ---
        self.username_input.returnPressed.connect(self.server_ip_input.setFocus)
        self.server_ip_input.returnPressed.connect(self.connect_btn.click)

    def accept_data(self):
        # Validate data before accepting
        username = self.username_input.text().strip()
        ip = self.server_ip_input.text().strip()

        if not username:
            # You could show an error, but for now, just set focus
            self.username_input.setFocus()
            return
        
        if not ip:
            self.server_ip_input.setFocus()
            return
        
        self.username = username
        self.server_ip = ip
        self.accept() # QDialog's built-in accept slot (closes the dialog)

    def get_data(self):
        # Public method to retrieve data after the dialog is closed
        return self.username, self.server_ip
# --- MAIN GUI CLASS ---
class LANCommClient(QMainWindow):
    new_message_signal = pyqtSignal(dict)
    new_file_signal = pyqtSignal(object)
    progress_signal = pyqtSignal(str, str, float, int, int)
    presenter_update_signal = pyqtSignal(object)
    
    connection_status_signal = pyqtSignal(bool) 
    
    _disconnecting = False

    def __init__(self, username, server_ip):
        super().__init__()
        self.running = True
        self.client_id = random.randint(1000, 9999)
        self._skip_close_dialog = False
        
        # --- MODIFIED: Receive data from the new dialog ---
        self.username = username
        self.server_ip = server_ip
        # ---
        
        self.clients = {}
        self.participant_tiles = {}
        self.client_connections = {}
        self.pinned_client_id = None
        self.pinned_screen_share = False  # Track if screen share is pinned
        self.screen_share_tile = None  # Special tile for screen share + presenter video
        self.current_presenter_id = None
        self.video_client = None
        self.audio_client = None
        self.chat_client = None
        self.file_client = None
        self.screen_client = None
        self.video_timer = QTimer(self); self.video_timer.timeout.connect(self.update_video_frames)
        self.screen_timer = QTimer(self); self.screen_timer.timeout.connect(self.update_screen_frame)

        # --- NEW: Unread message count ---
        self.unread_message_count = 0
        
        # --- NEW: Threadpool for workers ---
        self.threadpool = QThreadPool()

        # --- Connect signals ---
        self.new_message_signal.connect(self.on_new_message)
        self.new_file_signal.connect(self.on_new_file)
        self.progress_signal.connect(self.on_transfer_progress)
        self.presenter_update_signal.connect(self.on_presenter_update)
        self.connection_status_signal.connect(self.on_connection_status_changed)

        # --- qtawesome icons ---
        icon_color = 'white'
        icon_color_active = '#007bff' # Blue
        self.icon_video_off = qtawesome.icon('fa5s.video-slash', color=icon_color)
        self.icon_video_on = qtawesome.icon('fa5s.video', color=icon_color)
        self.icon_audio_off = qtawesome.icon('fa5s.microphone-slash', color=icon_color)
        self.icon_audio_on = qtawesome.icon('fa5s.microphone', color=icon_color)
        self.icon_screen_off = qtawesome.icon('fa5s.laptop', color=icon_color)
        self.icon_screen_on = qtawesome.icon('fa5s.stop-circle', color=icon_color)
        self.icon_chat = qtawesome.icon('fa5s.comments', color=icon_color)
        self.icon_chat_active = qtawesome.icon('fa5s.comments', color=icon_color_active) # Blue icon for "new"
        self.icon_hangup = qtawesome.icon('fa5s.phone-slash', color='white')
        self.icon_attach = qtawesome.icon('fa5s.paperclip', color='#b0b0b0') # Icon for file upload
        self.icon_send = qtawesome.icon('fa5s.paper-plane', color='white') # Icon for send
        self.init_ui() # Build UI
        connection_ok = self.connect_to_servers() # Connect
        if self.running and connection_ok:
            self.add_or_update_participant(self.client_id, f"{self.username} (You)")
            self.redraw_participant_grid()
            self.video_timer.start(int(1000 / VIDEO_FPS))
            self.screen_timer.start(int(1000 / SCREEN_FPS))
            self.populate_chat_with_history()

        elif self.running:
            QMessageBox.critical(self, "Connection Failed", "Could not connect. Check server IP and status.")
            self.running = False
            QTimer.singleShot(50, self.close)
    
    def hangup_and_close(self):
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
        
        # --- Main view stack (Video Panel) ---
        self.main_view_stack = QStackedWidget()
        
        # 1. Grid View (Index 0)
        self.scroll_area = QScrollArea(); self.scroll_area.setWidgetResizable(True); self.scroll_area.setStyleSheet("background-color: #1e1e1e;")
        self.participants_container = QWidget()
        self.grid_layout = QGridLayout(self.participants_container)
        self.grid_layout.setSpacing(10); self.grid_layout.setContentsMargins(10, 10, 10, 10)
        self.scroll_area.setWidget(self.participants_container)
        self.main_view_stack.addWidget(self.scroll_area) # <-- Index 0
        
        # 2. Pinned View (Index 1)
        self.pinned_view_widget = QWidget()
        pinned_layout = QHBoxLayout(self.pinned_view_widget)
        pinned_layout.setContentsMargins(0,0,0,0)
        self.pinned_tile_container = QWidget()
        self.pinned_tile_container.setLayout(QVBoxLayout())
        self.pinned_tile_container.layout().setContentsMargins(0,0,0,0)
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(240)
        sidebar_scroll.setStyleSheet("background-color: #1a1a1a; border: none;")
        sidebar_widget = QWidget()
        self.sidebar_layout = QVBoxLayout(sidebar_widget)
        self.sidebar_layout.setContentsMargins(5,5,5,5)
        self.sidebar_layout.setSpacing(5)
        self.sidebar_layout.addStretch(1)
        sidebar_scroll.setWidget(sidebar_widget)
        pinned_layout.addWidget(self.pinned_tile_container, 1)
        pinned_layout.addWidget(sidebar_scroll, 0)
        self.main_view_stack.addWidget(self.pinned_view_widget) # <-- Index 1

        # Screen share is now integrated into the main video view
        content_layout.addWidget(self.main_view_stack)
        
        # --- Controls Bar (Modified Layout and Size) ---
        controls_bar = QWidget(); controls_bar.setFixedHeight(70) # Increased height
        
        # --- NEW: Use a QHBoxLayout to center the group ---
        outer_controls_layout = QHBoxLayout(controls_bar)
        outer_controls_layout.setContentsMargins(10, 0, 10, 0) # Base padding
        outer_controls_layout.setSpacing(0) # Use spacing inside the central widget

        controls_group = QWidget()
        controls_layout = QHBoxLayout(controls_group)
        # Shifted controls group more to the right by adding stretch to its left
        controls_layout.setContentsMargins(0, 0, 0, 0) 
        controls_layout.setSpacing(15)
        
        # Increased icon and button sizes
        icon_size = QSize(28, 28) 
        btn_size = 56 # Increased size

        self.video_btn = QPushButton(); self.audio_btn = QPushButton()
        self.screen_btn = QPushButton(); self.hangup_btn = QPushButton()
        self.chat_btn = QPushButton()
        
        self.video_btn.setIcon(self.icon_video_off); self.video_btn.setToolTip("Start Video")
        self.audio_btn.setIcon(self.icon_audio_off); self.audio_btn.setToolTip("Unmute")
        self.screen_btn.setIcon(self.icon_screen_off); self.screen_btn.setToolTip("Share Screen")
        self.hangup_btn.setIcon(self.icon_hangup); self.hangup_btn.setToolTip("End Call")
        self.chat_btn.setIcon(self.icon_chat); self.chat_btn.setToolTip("Show Chat")
        
        button_list = [self.video_btn, self.audio_btn, self.screen_btn, self.hangup_btn]
        for btn in button_list:
            btn.setFixedSize(btn_size, btn_size); btn.setIconSize(icon_size); btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"QPushButton {{ border-radius: {btn_size // 2}px; padding: 5px; border: 1px solid #5a5a5a; background-color: #3a3a3a; }} QPushButton:hover {{ background-color: #4f4f4f; }}")
        
        self.chat_btn.setFixedSize(btn_size, btn_size); self.chat_btn.setIconSize(icon_size); self.chat_btn.setCursor(Qt.PointingHandCursor)
        self.chat_btn.setStyleSheet(f"QPushButton {{ border-radius: {btn_size // 2}px; padding: 5px; border: 1px solid #5a5a5a; background-color: #3a3a3a; }} QPushButton:hover {{ background-color: #4f4f4f; }}")

        # --- Reorganized Layout: Controls (Center Group Left) ---
        controls_layout.addWidget(self.video_btn); controls_layout.addWidget(self.audio_btn)
        controls_layout.addWidget(self.screen_btn); 
        controls_layout.addWidget(self.hangup_btn)
        controls_layout.addSpacing(25)
        self.reaction_emojis = ["👍", "😂", "😮", "🔥", "💯", "🎉"]
        self.reaction_buttons = []
        
        for emoji in self.reaction_emojis:
            btn = QPushButton(emoji)
            btn.setFixedSize(btn_size, btn_size) # Same size as control buttons
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #3a3a3a;
                    border: 1px solid #5a5a5a;
                    border-radius: {btn_size // 2}px;
                    font-size: 24pt; /* Slightly larger emoji */
                    padding-bottom: 5px;
                }}
                QPushButton:hover {{
                    background-color: #4f4f4f;
                    border: 1px solid #6a6a6a;
                }}
            """)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"React with {emoji}")
            btn.clicked.connect(lambda checked, e=emoji: self.send_reaction(e))
            controls_layout.addWidget(btn)
            self.reaction_buttons.append(btn)
        
        # Add stretch to the LEFT of the controls_group to push it more right
        outer_controls_layout.addStretch(1) 
        outer_controls_layout.addWidget(controls_group)
        outer_controls_layout.addStretch(1) # Balance the stretch
        chat_container = QWidget()
        chat_layout = QHBoxLayout(chat_container)
        chat_layout.setContentsMargins(0, 0, 50, 0) # Adjusted right margin to 50px
        chat_layout.addStretch()
        chat_layout.addWidget(self.chat_btn)
        outer_controls_layout.addWidget(chat_container)
        
        content_layout.addWidget(controls_bar)
        
        main_layout.addWidget(content_widget)

        # --- Right Chat Panel ---
        self.right_panel = QWidget(); self.right_panel.setFixedWidth(350); self.right_panel.setStyleSheet("background-color: #2b2b2b;")
        right_panel_layout = QVBoxLayout(self.right_panel)
        right_panel_layout.setContentsMargins(10,10,10,10)
        right_panel_layout.setSpacing(8)
        self.chat_list_widget = QListWidget()
        self.chat_list_widget.setStyleSheet("background-color: #2b2b2b; border: none;")
        self.chat_list_widget.setSpacing(5)
        self.chat_list_widget.setWordWrap(True)
        self.chat_list_widget.setUniformItemSizes(False)
        self.chat_list_widget.setSelectionMode(QListWidget.NoSelection)
        self.chat_progress_bar = QProgressBar();         self.chat_progress_bar.setVisible(False)
        self.chat_progress_bar.setTextVisible(True)
        self.chat_progress_bar.setStyleSheet("QProgressBar { border: 1px solid #5a5a5a; border-radius: 5px; background-color: #3c3c3c; text-align: center; color: white;} QProgressBar::chunk { background-color: #3aa76d; border-radius: 5px;}")
        
        input_widget = QWidget()
        input_layout = QVBoxLayout(input_widget)
        input_layout.setContentsMargins(0,0,0,0)
        input_layout.setSpacing(5)
        
        # Recipient selection row (at bottom, above input)
        recipient_row = QHBoxLayout()
        recipient_row.setContentsMargins(0, 0, 0, 0)
        recipient_row.setSpacing(5)
        recipient_label = QLabel("To:")
        recipient_label.setStyleSheet("color: #a0a0a0; font-size: 10pt;")
        self.chat_recipient_combo = QComboBox()
        self.chat_recipient_combo.setStyleSheet("""
            QComboBox {
                background-color: #3c3c3c; 
                border-radius: 6px; 
                padding: 5px; 
                color: #f0f0f0; 
                min-width: 120px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 4px solid #a0a0a0;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c;
                selection-background-color: #007bff;
                color: #f0f0f0;
            }
        """)
        self.chat_recipient_combo.addItem("Everyone", None)  # None = public message
        recipient_row.addWidget(recipient_label)
        recipient_row.addWidget(self.chat_recipient_combo)
        recipient_row.addStretch()
        
        # Message input row
        message_row = QHBoxLayout()
        message_row.setContentsMargins(0,0,0,0)
        message_row.setSpacing(5)
        self.attach_file_btn = QPushButton(self.icon_attach, "")
        self.attach_file_btn.setFixedSize(32, 32); self.attach_file_btn.setIconSize(QSize(16, 16))
        self.attach_file_btn.setStyleSheet("background-color: #3c3c3c; border-radius: 6px;")
        self.attach_file_btn.setCursor(Qt.PointingHandCursor)
        self.attach_file_btn.setToolTip("Upload a file")
        self.message_input = QLineEdit(); self.message_input.setPlaceholderText("Type a message...")
        self.message_input.setStyleSheet("background-color: #3c3c3c; border-radius: 6px; padding: 5px; color: #f0f0f0; height: 22px;")
        self.send_btn = QPushButton(self.icon_send, "")
        self.send_btn.setFixedSize(32, 32); self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setStyleSheet("background-color: #007bff; color: white; border-radius: 6px;")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        message_row.addWidget(self.attach_file_btn)
        message_row.addWidget(self.message_input)
        message_row.addWidget(self.send_btn)
        
        input_layout.addLayout(recipient_row)
        input_layout.addLayout(message_row)
        
        right_panel_layout.addWidget(self.chat_list_widget)
        right_panel_layout.addWidget(self.chat_progress_bar)
        right_panel_layout.addWidget(input_widget)
        main_layout.addWidget(self.right_panel)
        self.right_panel.hide() 

        # --- Connect Signals ---
        self.video_btn.clicked.connect(self.toggle_camera); self.audio_btn.clicked.connect(self.toggle_audio)
        self.screen_btn.clicked.connect(self.toggle_screen_share)
        self.hangup_btn.clicked.connect(self.hangup_and_close)
        self.chat_btn.clicked.connect(self.toggle_right_panel)
        self.message_input.returnPressed.connect(self.send_message)
        self.send_btn.clicked.connect(self.send_message)
        self.attach_file_btn.clicked.connect(self.trigger_upload_file)

        # --- Final Button Styling ---
        self.update_button_style(self.video_btn, False, btn_size)
        self.update_button_style(self.audio_btn, False, btn_size)
        self.update_button_style(self.screen_btn, False, btn_size)
        self.update_button_style(self.chat_btn, False, btn_size)
        self.hangup_btn.setStyleSheet(f"QPushButton {{ border-radius: {btn_size // 2}px; padding: 5px; border: 1px solid #b02a2f; background-color: #d93d43; }} QPushButton:hover {{ background-color: #e15258; }}")

    def connect_to_servers(self):
        self.statusBar().showMessage('Connecting...')
        all_connected = True
        self.client_connections = {}

        client_configs = [
            ('video', VideoClient, [], {}),
            ('audio', AudioClient, [], {}),
            ('chat', ChatClient, [self.username], {}),
            ('file', FileClient, [self.username], {}), # <-- Uses the NEW FileClient
            ('screen', ScreenClient, [lambda s: self.presenter_update_signal.emit(s)], {})
        ]

        for name, client_class, extra_init_args, connect_kwargs in client_configs:
            try:
                instance_args = (self.client_id, self.server_ip) + tuple(extra_init_args)
                client = client_class(*instance_args)
                
                if name == 'chat':
                    client.register_callback(lambda msg: self.new_message_signal.emit(msg))
                
                if name == 'file':
                    client.register_new_file_callback(lambda n: self.new_file_signal.emit(n))
                    client.register_progress_callback(lambda o, f, p, c, t: self.progress_signal.emit(o, f, p, c, t))
                    client.register_connection_status_callback(lambda s: self.connection_status_signal.emit(s))

                if client.connect(**connect_kwargs):
                    self.client_connections[name] = client
                    print(f"[GUI] Connected to {name.capitalize()} Server.")
                    if name == 'audio': client.start_speakers()
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
    
    @pyqtSlot(bool)
    def on_connection_status_changed(self, is_connected):
        """
        Handles connection status updates from clients (especially FileClient).
        """
        if not self.running: return
        
        if is_connected:
            self.statusBar().showMessage('File service reconnected.', 3000)
            # Re-sync chat and file history after a reconnect
            self.populate_chat_with_history()
        else:
            self.statusBar().showMessage('File service connection lost! Attempting to reconnect...', 5000)

    def update_button_style(self, button, is_active, btn_size):
        border_radius = btn_size // 2
        base_style = f"""
            QPushButton {{ border-radius: {border_radius}px; padding: 5px; border: 1px solid #5a5a5a; }}
            QPushButton:hover {{ background-color: #4f4f4f; }}
            QPushButton:disabled {{ background-color: #2a2a2a; border: 1px solid #404040; }}
        """
        active_bg = "#d93d43"; inactive_bg = "#3a3a3a"; active_border = "#b02a2f"
        
        # NOTE: Hangup button is styled permanently red in init_ui now
        if button == self.hangup_btn: return 
        
        if button == self.chat_btn:
             button.setStyleSheet(base_style + f"QPushButton {{ background-color: {inactive_bg}; }}")
             self._update_chat_badge() # Ensure badge is correct
        
        else:
            if is_active:
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {active_bg}; border: 1px solid {active_border}; }}")
            else:
                button.setStyleSheet(base_style + f"QPushButton {{ background-color: {inactive_bg}; }}")

    def toggle_right_panel(self):
        is_visible = not self.right_panel.isVisible()
        self.right_panel.setVisible(is_visible)
        self.chat_btn.setToolTip("Hide Chat" if is_visible else "Show Chat")
        if is_visible:
            self.unread_message_count = 0
            self.message_input.setFocus()
        # Get the current button size used in init_ui
        btn_size = self.chat_btn.width()
        self.update_button_style(self.chat_btn, is_visible, btn_size)

    def _add_widget_to_chat(self, widget):
        """Adds a custom widget to the chat QListWidget."""
        item = QListWidgetItem(self.chat_list_widget)
        item.setSizeHint(widget.sizeHint())
        self.chat_list_widget.addItem(item)
        self.chat_list_widget.setItemWidget(item, widget)
        QTimer.singleShot(10, lambda: self.chat_list_widget.scrollToBottom())
    def _update_chat_badge(self):
        """Updates the chat button icon to show the unread message count."""
        if not hasattr(self, 'chat_btn'): return 
        
        # Use the icon size set in init_ui
        icon_size_val = self.chat_btn.iconSize().width()
        
        if self.unread_message_count == 0:
            if self.right_panel.isVisible():
                self.chat_btn.setIcon(self.icon_chat_active)
                # Keep the original active style for a clean look
                self.chat_btn.setStyleSheet(self.chat_btn.styleSheet()) 
            else:
                self.chat_btn.setIcon(self.icon_chat) 
                # Keep the original inactive style
                self.chat_btn.setStyleSheet(self.chat_btn.styleSheet())
            
            self.chat_btn.setToolTip("Show Chat")
        else:
            try:
                # Use icon_chat_active for the base icon when badged
                base_pixmap = self.icon_chat_active.pixmap(QSize(icon_size_val, icon_size_val)) 
                
                # Create a larger pixmap to accommodate the badge
                badge_offset_x = 8 
                badged_pixmap = QPixmap(base_pixmap.size().width() + badge_offset_x, base_pixmap.size().height())
                badged_pixmap.fill(Qt.transparent)
                
                painter = QPainter(badged_pixmap)
                painter.setRenderHint(QPainter.Antialiasing)
                
                # Draw the base icon on the left
                painter.drawPixmap(0, 0, base_pixmap)
                
                badge_diameter = 16
                # Position badge on the top right of the icon area
                badge_x = base_pixmap.width() - 5 
                badge_y = 0
                
                painter.setBrush(QColor("red"))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(badge_x, badge_y, badge_diameter, badge_diameter)
                painter.setPen(QColor("white"))
                font = QFont(); font.setBold(True); font.setPixelSize(10)
                painter.setFont(font)
                text = str(self.unread_message_count)
                painter.drawText(QRect(badge_x, badge_y, badge_diameter, badge_diameter), Qt.AlignCenter, text)
                
                painter.end()
                
                self.chat_btn.setIcon(QIcon(badged_pixmap))
                self.chat_btn.setToolTip(f"Show Chat ({self.unread_message_count} new)")
                self.chat_btn.setStyleSheet(self.chat_btn.styleSheet()) # Preserve the button background style
            
            except Exception as e:
                print(f"[GUI ERROR] Failed to draw badge: {e}")
                self.chat_btn.setIcon(self.icon_chat_active)
                self.chat_btn.setToolTip(f"Show Chat ({self.unread_message_count} new)")


    def add_or_update_participant(self, client_id, username):
        self.clients[client_id] = {'username': username}
        tile_exists = client_id in self.participant_tiles
        needs_redraw = False
        if not tile_exists:
            tile = ParticipantTile(username=username, client_id=client_id)
            tile.doubleClicked.connect(self.on_tile_double_clicked)
            self.participant_tiles[client_id] = tile
            needs_redraw = True
        elif self.participant_tiles[client_id].username != username:
            self.participant_tiles[client_id].username = username
            self.participant_tiles[client_id].update()
        return needs_redraw

    def remove_participant(self, client_id):
        tile_removed = False
        if client_id in self.participant_tiles:
            tile_to_remove = self.participant_tiles.pop(client_id)
            if tile_to_remove: tile_to_remove.deleteLater()
            tile_removed = True
        if client_id in self.clients:
            del self.clients[client_id]
        return tile_removed

    def redraw_participant_grid(self):
        """
        This function now moves all tiles to the correct
        layout (grid OR pinned) based on self.pinned_client_id.
        Includes screen share tile in grid when present.
        """
        if threading.current_thread() != threading.main_thread():
            QTimer.singleShot(0, self.redraw_participant_grid)
            return
        
        while (item := self.grid_layout.takeAt(0)) is not None:
            if item.widget(): 
                item.widget().setParent(None) 
        
        while (item := self.pinned_tile_container.layout().takeAt(0)) is not None:
            if item.widget(): 
                item.widget().setParent(None)

        while (item := self.sidebar_layout.takeAt(0)) is not None:
            if item.widget(): 
                item.widget().setParent(None)
            elif item.spacerItem():
                self.sidebar_layout.removeItem(item) 

        tiles = list(self.participant_tiles.values())
        
        # Check if screen share is pinned
        if self.pinned_screen_share and self.screen_share_tile is not None:
            print("[GUI] Redrawing in PINNED mode for screen share")
            # Screen share is pinned - show it in main area
            self.pinned_tile_container.layout().addWidget(self.screen_share_tile)
            # Show all participant tiles in sidebar (including presenter's video)
            for tile in tiles:
                self.sidebar_layout.addWidget(tile)
            self.sidebar_layout.addStretch(1)
        
        elif self.pinned_client_id is not None and self.pinned_client_id in self.participant_tiles:
            print(f"[GUI] Redrawing in PINNED mode for {self.pinned_client_id}")
            for tile in tiles:
                if tile.client_id == self.pinned_client_id:
                    self.pinned_tile_container.layout().addWidget(tile)
                else:
                    self.sidebar_layout.addWidget(tile)
            # Add screen share tile to sidebar if present
            if self.screen_share_tile is not None:
                self.sidebar_layout.addWidget(self.screen_share_tile)
            self.sidebar_layout.addStretch(1) 
        
        else:
            print("[GUI] Redrawing in GRID mode")
            self.pinned_client_id = None 
            self.pinned_screen_share = False  # Reset if somehow set
            
            self.grid_layout.setRowStretch(1, 0)
            self.grid_layout.setColumnStretch(1, 0)

            all_items = tiles.copy()
            if self.screen_share_tile is not None:
                all_items.insert(0, self.screen_share_tile)  # Add screen share as first item
            
            n = len(all_items)
            if n == 0: 
                pass
            else:
                cols = 2 if n <= 4 else 3 if n <= 9 else 4
                for i, tile in enumerate(all_items):
                    self.grid_layout.addWidget(tile, i // cols, i % cols)
        
        self.participants_container.updateGeometry()
        self.scroll_area.updateGeometry()
        self.pinned_view_widget.updateGeometry()

    def toggle_camera(self):
        if not self.video_client: return
        btn_size = self.video_btn.width()
        if self.video_client.sending:
            self.video_client.stop_camera()
            self.video_btn.setIcon(self.icon_video_off); self.video_btn.setToolTip("Start Video")
            self.update_button_style(self.video_btn, False, btn_size)
        elif self.video_client.start_camera():
            self.video_btn.setIcon(self.icon_video_on); self.video_btn.setToolTip("Stop Video")
            self.update_button_style(self.video_btn, True, btn_size)

    def toggle_audio(self):
        if not self.audio_client: return
        btn_size = self.audio_btn.width() 
        if self.audio_client.sending:
            self.audio_client.stop_microphone()
            self.audio_btn.setIcon(self.icon_audio_off); self.audio_btn.setToolTip("Unmute")
            self.update_button_style(self.audio_btn, False, btn_size)
        elif self.audio_client.start_microphone():
            self.audio_btn.setIcon(self.icon_audio_on); self.audio_btn.setToolTip("Mute")
            self.update_button_style(self.audio_btn, True, btn_size)

    def toggle_screen_share(self):
        if not self.screen_client: return
        if self.screen_client.is_presenting:
            self.screen_client.stop_sharing()
        else:
            self.screen_client.start_sharing()

    def update_video_frames(self):
        if not self.video_client or not self.running: 
            return
        local_tile = self.participant_tiles.get(self.client_id)
        if local_tile:
            if self.video_client.sending and self.video_client.cap and self.video_client.cap.isOpened():
                ret, frame = self.video_client.cap.read()
                if ret: 
                    local_tile.update_frame(cv2.flip(frame, 1))
                else: 
                    local_tile.clear_frame()
            else: 
                local_tile.clear_frame()
        
        try: 
            remote_frames = self.video_client.get_frames()
        except Exception as e: 
            remote_frames = {}
        
        cids_processed_this_cycle = {self.client_id}
        for cid, frame in remote_frames.items():
            if cid == self.client_id: 
                continue
            tile = self.participant_tiles.get(cid)
            if tile:
                tile.update_frame(frame)
                cids_processed_this_cycle.add(cid)
        
        for cid, tile in list(self.participant_tiles.items()):
            if cid not in cids_processed_this_cycle:
                tile.clear_frame()

    @pyqtSlot(object)
    def on_presenter_update(self, status):
        """
        Handles screen share status by adding/removing screen share tile.
        On receiver side: shows screen share + presenter's video together.
        On sender side: just updates button state.
        """
        if not self.running: 
            return
        presenter_id = status.get("presenter_id")
        self.current_presenter_id = presenter_id
        self.screen_btn.setEnabled(True) 
        btn_size = self.screen_btn.width() # Get current size

        if presenter_id is None:
            # --- NO ONE IS PRESENTING ---
            self.screen_btn.setIcon(self.icon_screen_off)
            self.screen_btn.setToolTip("Share Screen")
            self.update_button_style(self.screen_btn, False, btn_size)
            
            # Remove screen share tile if it exists
            if self.screen_share_tile is not None:
                self.screen_share_tile.deleteLater()
                self.screen_share_tile = None
                self.pinned_screen_share = False  # Unpin if it was pinned
                if self.pinned_client_id is None:
                    self.main_view_stack.setCurrentIndex(0)  # Switch to grid if nothing else pinned
                QTimer.singleShot(0, self.redraw_participant_grid)

        else:
            # --- SOMEONE IS PRESENTING ---
            if presenter_id == self.client_id:
                # Sender side: just update button, don't show tile
                self.screen_btn.setIcon(self.icon_screen_on)
                self.screen_btn.setToolTip("Stop Sharing")
                self.update_button_style(self.screen_btn, True, btn_size)
                
                # Remove screen share tile if it exists (sender doesn't need to see it)
                if self.screen_share_tile is not None:
                    self.screen_share_tile.deleteLater()
                    self.screen_share_tile = None
                    QTimer.singleShot(0, self.redraw_participant_grid)
            else:
                # Receiver side: show screen share + presenter's video
                presenter_name = self.clients.get(presenter_id, {}).get('username', f"User {presenter_id}")
                self.screen_btn.setIcon(self.icon_screen_off)
                self.screen_btn.setToolTip(f"{presenter_name} is presenting")
                self.update_button_style(self.screen_btn, False, btn_size)
                self.screen_btn.setEnabled(False)
                
                # Create or update screen share tile
                if self.screen_share_tile is None:
                    self.screen_share_tile = ScreenShareTile(presenter_name=presenter_name, presenter_id=presenter_id)
                    self.screen_share_tile.doubleClicked.connect(self.on_screen_share_double_clicked)
                    # Pin screen share by default
                    self.pinned_screen_share = True
                    self.pinned_client_id = None  # Unpin any video if pinned
                    self.main_view_stack.setCurrentIndex(1)  # Switch to pinned view
                    self.screen_share_tile.update_pin_status(True)  # Update indicator
                    QTimer.singleShot(0, self.redraw_participant_grid)
                elif self.screen_share_tile.presenter_id != presenter_id:
                    # Presenter changed, recreate tile
                    self.screen_share_tile.deleteLater()
                    self.screen_share_tile = ScreenShareTile(presenter_name=presenter_name, presenter_id=presenter_id)
                    self.screen_share_tile.doubleClicked.connect(self.on_screen_share_double_clicked)
                    # Pin screen share by default
                    self.pinned_screen_share = True
                    self.pinned_client_id = None  # Unpin any video if pinned
                    self.main_view_stack.setCurrentIndex(1)  # Switch to pinned view
                    self.screen_share_tile.update_pin_status(True)  # Update indicator
                    QTimer.singleShot(0, self.redraw_participant_grid)
    
    def update_screen_frame(self):
        """Update the screen share tile with new screen frame data."""
        if not self.screen_client or self.screen_client.is_presenting or not self.running: 
            return
        frame_data = self.screen_client.get_frame()
        if frame_data == 'EMPTY' or frame_data is None: 
            return
        
        # Update screen share tile if it exists (receiver side)
        if self.screen_share_tile is not None:
            self.screen_share_tile.update_screen_frame(frame_data)

    @pyqtSlot(dict)
    def on_new_message(self, msg):
        """
        [REWRITE] Handles new messages from the chat server.
        Adds a custom widget to the chat list.
        """
        if not self.running: return
        msg_type = msg.get('type')

        widget_to_add = None
        
        if msg_type == 'user_list':
            current_cids_in_list = set()
            users = msg.get('users', [])
            redraw_needed = False
            
            # Update recipient combo box
            self.chat_recipient_combo.clear()
            self.chat_recipient_combo.addItem("Everyone", None)  # Public message option
            
            for user in users:
                user_id = user.get('client_id'); user_name = user.get('username')
                if user_id is None: continue
                current_cids_in_list.add(user_id)
                local_name = (f"{user_name} (You)" if user_id == self.client_id else user_name)
                if self.add_or_update_participant(user_id, local_name):
                    redraw_needed = True
                
                # Add to recipient combo box (exclude self)
                if user_id != self.client_id:
                    display_name = user_name
                    self.chat_recipient_combo.addItem(display_name, user_id)
            
            ids_to_remove = set(self.participant_tiles.keys()) - current_cids_in_list
            for cid in ids_to_remove:
                if self.remove_participant(cid):
                    redraw_needed = True
            
            if redraw_needed or not self.participant_tiles or set(self.participant_tiles.keys()) == {self.client_id}:
                QTimer.singleShot(0, self.redraw_participant_grid)
        
        elif msg_type == 'message':
            is_self = msg.get('client_id') == self.client_id
            username = msg.get('username', '???')
            text = msg.get('text', '')
            is_private = msg.get('is_private', False)
            recipient_id = msg.get('recipient_id')
            sender_id = msg.get('sender_id')
            
            # Get recipient/sender names for private messages
            recipient_name = None
            sender_name = None
            if is_private:
                if is_self and recipient_id:
                    # Find recipient name - try clients dict first, then check message history
                    recipient_info = self.clients.get(recipient_id, {})
                    recipient_name = recipient_info.get('username')
                    if not recipient_name:
                        # Try to find in chat history (for users who left)
                        try:
                            chat_history = self.chat_client.get_message_history() if self.chat_client else []
                            for hist_msg in reversed(chat_history):
                                if hist_msg.get('type') == 'user_list':
                                    users = hist_msg.get('users', [])
                                    for u in users:
                                        if u.get('client_id') == recipient_id:
                                            recipient_name = u.get('username', f'User {recipient_id}')
                                            break
                                    if recipient_name:
                                        break
                        except:
                            pass
                    if not recipient_name:
                        recipient_name = f'User {recipient_id}'
                    # Remove "(You)" suffix if present
                    recipient_name = recipient_name.replace(' (You)', '')
                elif not is_self and sender_id:
                    # Find sender name (already in username, but for consistency)
                    sender_name = username
            
            widget_to_add = ChatMessageWidget(
                username, text, 
                is_self=is_self, 
                is_private=is_private,
                recipient_name=recipient_name,
                sender_name=sender_name
            )
        
        elif msg_type == 'reaction':
            # Handle reaction - show emoji above participant tile
            reaction_client_id = msg.get('client_id')
            emoji = msg.get('emoji', '')
            if reaction_client_id and emoji:
                tile = self.participant_tiles.get(reaction_client_id)
                if tile:
                    tile.show_reaction(emoji)
            return  # Don't add reaction to chat list
        
        elif msg_type == 'system':
            widget_to_add = ChatMessageWidget(None, msg.get('text', ''), is_system=True)
        
        if widget_to_add:
            self._add_widget_to_chat(widget_to_add)
            
            # --- NEW: Update badge ---
            if not self.right_panel.isVisible():
                self.unread_message_count += 1
                self._update_chat_badge()

    @pyqtSlot(object)
    def on_new_file(self, notification):
        """
        [REWRITE] Handles new file notifications or list refreshes.
        """
        if not self.running: return
        if notification is None:
            self.statusBar().showMessage(f"File list refreshed.", 3000)
            self.populate_chat_with_history()
            return


        if notification.get('type') == 'new_file':
            # Uploader ID isn't sent, so we compare names
            is_self = notification.get('uploader') == self.username
            widget = ChatFileWidget(notification.get('uploader'), notification, is_self=is_self)
            
            widget.download_requested.connect(self.on_download_requested_from_widget)
            
            self._add_widget_to_chat(widget)
            if not self.right_panel.isVisible():
                self.unread_message_count += 1
                self._update_chat_badge()

    @pyqtSlot(str, str, float, int, int)
    def on_transfer_progress(self, op, filename, progress, current, total):
        if not self.running: return
        self.chat_progress_bar.setVisible(True)
        self.chat_progress_bar.setValue(int(progress))
        self.chat_progress_bar.setFormat(f"{op.capitalize()}: {progress:.0f}%")
        
        if progress >= 100:
            QTimer.singleShot(2500, lambda: self.chat_progress_bar.setVisible(False))
            if op == 'download': 
                self.statusBar().showMessage(f"'{filename}' downloaded to Downloads folder.", 4000)
                for i in range(self.chat_list_widget.count()):
                    item = self.chat_list_widget.item(i)
                    widget = self.chat_list_widget.itemWidget(item)
                    if isinstance(widget, ChatFileWidget) and widget.filename == filename:
                        widget.download_btn.setText(" Download")
                        widget.download_btn.setEnabled(True)
                        break

            elif op == 'upload': 
                print("[GUI] Upload complete. Server will notify all clients.")
        
        elif progress < 0: 
            self.chat_progress_bar.setFormat(f"{op.capitalize()} Failed")
            QTimer.singleShot(3000, lambda: self.chat_progress_bar.setVisible(False))
            if op == 'download':
                for i in range(self.chat_list_widget.count()):
                    item = self.chat_list_widget.item(i)
                    widget = self.chat_list_widget.itemWidget(item)
                    if isinstance(widget, ChatFileWidget) and widget.filename == filename:
                        widget.download_btn.setText(" Download")
                        widget.download_btn.setEnabled(True)
                        break

    def send_message(self, ):
        text_to_send = self.message_input.text().strip()
        if self.chat_client and text_to_send:
            # Get recipient_id from combo box (None = public message)
            recipient_id = self.chat_recipient_combo.currentData()
            self.chat_client.send_message(text_to_send, recipient_id=recipient_id)
            self.message_input.clear()
    
    def send_reaction(self, emoji):
        """Send a reaction emoji that will appear above the user's tile."""
        if self.chat_client:
            self.chat_client.send_reaction(emoji)
            # Also show reaction on own tile immediately
            own_tile = self.participant_tiles.get(self.client_id)
            if own_tile:
                own_tile.show_reaction(emoji)

    def trigger_upload_file(self):
        """
        [REWRITE]
        Called by the paperclip button.
        Uses a QRunnable worker to run the upload in a threadpool
        and connects its 'finished' signal to the on_upload_finished slot.
        """
        if not self.file_client: return
        filepath, _ = QFileDialog.getOpenFileName(self, "Select File to Upload")
        if filepath:
            self.chat_progress_bar.setVisible(True); self.chat_progress_bar.setValue(0)
            self.chat_progress_bar.setFormat("Uploading: 0%")
            worker = UploadWorker(self.file_client, filepath)
            worker.signals.finished.connect(self.on_upload_finished)
            self.threadpool.start(worker)
    @pyqtSlot(object)
    def on_upload_finished(self, file_info):
        """
        [REWRITE] This function is called when the UploadWorker is done.
        It NO LONGER adds the file locally. It just reports success
        and waits for the server's broadcast message, which will
        trigger on_new_file.
        """
        if file_info and isinstance(file_info, dict):
            print(f"[GUI] Upload finished. Waiting for server broadcast.") 
            
            self.on_transfer_progress('upload', 
                                      file_info.get('filename', 'File'), 
                                      100, 
                                      file_info.get('filesize', 0), 
                                      file_info.get('filesize', 0))
            
        else:
            print("[GUI] Upload failed (received None from worker).")
            self.on_transfer_progress('upload', 'Unknown file', -1, 0, 0) 

    @pyqtSlot(str)
    def on_download_requested_from_widget(self, file_id):
        """Triggers a file download in a new thread."""
        if not self.file_client: return
        
        file_info = None
        try:
            files = self.file_client.get_available_files()
            file_info = next((f for f in files if f.get('file_id') == file_id), None)
        except Exception as e:
            print(f"[GUI ERROR] Could not get file info: {e}")
            
        if not file_info:
             print(f"[GUI ERROR] File ID {file_id} not found in client's list.")
             for i in range(self.chat_list_widget.count()):
                 item = self.chat_list_widget.item(i)
                 widget = self.chat_list_widget.itemWidget(item)
                 if isinstance(widget, ChatFileWidget) and widget.file_id == file_id:
                     widget.download_btn.setText(" Download")
                     widget.download_btn.setEnabled(True)
                     break
             return
            
        filename = file_info.get('filename', f"download_{file_id}")
        save_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)
        
        self.chat_progress_bar.setVisible(True); self.chat_progress_bar.setValue(0)
        threading.Thread(target=self.file_client.download_file, args=(file_id, save_path), daemon=True).start()

    def populate_chat_with_history(self):
        """
        Fetches chat and file history from clients,
        merges and sorts them, and populates the chat list.
        """
        if not self.chat_client or not self.file_client:
            print("[GUI] Clients not ready for history population.")
            return
            
        print("[GUI] Populating chat with history...")
        self.chat_list_widget.clear()
        
        try:
            chat_history = self.chat_client.get_message_history()
            file_history = self.file_client.get_available_files()
        except Exception as e:
            print(f"[GUI ERROR] Failed to get history: {e}")
            return
            
        combined_history = []
        for msg in chat_history:
            msg['sort_key'] = msg.get('timestamp', '1970-01-01T00:00:00')
            msg['item_type'] = 'chat'
            combined_history.append(msg)
            
        for f in file_history:
            f['sort_key'] = f.get('timestamp', '1970-01-01T00:00:00')
            f['item_type'] = 'file'
            combined_history.append(f)
            
        # Sort by timestamp
        try:
            combined_history.sort(key=lambda x: x['sort_key'])
        except Exception as e:
            print(f"[GUI ERROR] Failed to sort history: {e}")
            
        # Add all items to the list
        for item in combined_history:
            widget_to_add = None
            if item['item_type'] == 'chat':
                msg_type = item.get('type')
                if msg_type == 'message':
                    is_self = item.get('client_id') == self.client_id
                    is_private = item.get('is_private', False)
                    recipient_id = item.get('recipient_id')
                    sender_id = item.get('sender_id')
                    
                    # Get recipient/sender names for private messages
                    recipient_name = None
                    sender_name = None
                    if is_private:
                        if is_self and recipient_id:
                            # Find recipient name - try clients dict first, then check message history
                            recipient_info = self.clients.get(recipient_id, {})
                            recipient_name = recipient_info.get('username')
                            if not recipient_name:
                                # Try to find in chat history (for users who left)
                                for hist_msg in reversed(combined_history):
                                    if hist_msg.get('item_type') == 'chat' and hist_msg.get('type') == 'user_list':
                                        users = hist_msg.get('users', [])
                                        for u in users:
                                            if u.get('client_id') == recipient_id:
                                                recipient_name = u.get('username', f'User {recipient_id}')
                                                break
                                        if recipient_name:
                                            break
                                if not recipient_name:
                                    recipient_name = f'User {recipient_id}'
                                recipient_name = recipient_name.replace(' (You)', '')
                            elif not is_self and sender_id:
                                sender_name = item.get('username', f'User {sender_id}')
                    
                    widget_to_add = ChatMessageWidget(
                        item.get('username'), 
                        item.get('text'), 
                        is_self=is_self,
                        is_private=is_private,
                        recipient_name=recipient_name,
                        sender_name=sender_name
                    )
                elif msg_type == 'system':
                    widget_to_add = ChatMessageWidget(None, item.get('text'), is_system=True)
            
            elif item['item_type'] == 'file':
                is_self = item.get('uploader') == self.username
                widget_to_add = ChatFileWidget(item.get('uploader'), item, is_self=is_self)
                widget_to_add.download_requested.connect(self.on_download_requested_from_widget)
            
            if widget_to_add:
                self._add_widget_to_chat(widget_to_add)
        
        print(f"[GUI] History populated with {len(combined_history)} items.")
        self.chat_list_widget.scrollToBottom()


    def refresh_files(self):
        """
        Starts a background thread to fetch the file list.
        The UI update is handled by the on_new_file(None) callback.
        """
        if not self.file_client or not self.running: return
        print("[GUI] Queuing file list refresh.")
        threading.Thread(target=self.file_client.refresh_file_list, daemon=True).start()
    @pyqtSlot()
    def on_screen_share_double_clicked(self):
        """Handle double-click on screen share tile to pin/unpin it."""
        if self.pinned_screen_share:
            # Unpin screen share
            print("[GUI] Unpinning screen share")
            self.pinned_screen_share = False
            if self.pinned_client_id is None:
                self.main_view_stack.setCurrentIndex(0)  # Switch to grid view
            # Update pin indicator
            if self.screen_share_tile is not None:
                self.screen_share_tile.update_pin_status(False)
        else:
            # Pin screen share
            print("[GUI] Pinning screen share")
            self.pinned_screen_share = True
            self.pinned_client_id = None  # Unpin any video if pinned
            self.main_view_stack.setCurrentIndex(1)  # Switch to pinned view
            # Update pin indicator
            if self.screen_share_tile is not None:
                self.screen_share_tile.update_pin_status(True)
        
        self.redraw_participant_grid()
    
    @pyqtSlot(int)
    def on_tile_double_clicked(self, client_id):
        """
        Handles pinning/unpinning a video.
        If screen share is pinned, unpin it first.
        """
        if client_id == self.pinned_client_id:
            # --- UNPINNING VIDEO ---
            print(f"[GUI] Unpinning client {client_id}")
            self.pinned_client_id = None
            self.main_view_stack.setCurrentIndex(0)  # Switch to grid view
        else:
            # --- PINNING VIDEO ---
            print(f"[GUI] Pinning client {client_id}")
            self.pinned_client_id = client_id
            if self.pinned_screen_share:
                self.pinned_screen_share = False
                if self.screen_share_tile is not None:
                    self.screen_share_tile.update_pin_status(False)
            self.main_view_stack.setCurrentIndex(1)  # Switch to pinned view
        
        self.redraw_participant_grid()
    def closeEvent(self, event, skip_dialog=False):
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
    app = QApplication(sys.argv)
    login_dialog = LoginDialog(default_ip='127.0.0.1')
    if login_dialog.exec_() == QDialog.Accepted:
        username, server_ip = login_dialog.get_data()
        
        window = None
        try:
            window = LANCommClient(username, server_ip) 
            
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
    
    else:
        print("Login cancelled. Exiting.")
        sys.exit(0)