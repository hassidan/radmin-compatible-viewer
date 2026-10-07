"""Cross-platform address book and application entry point."""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import Qt, QStandardPaths, QLockFile, QTimer, QSize, QObject, QEvent, Signal
from PySide6.QtGui import QColor, QBrush, QAction, QActionGroup, QIcon
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QTreeWidget, QTreeWidgetItem, QToolBar, QDialog, QFormLayout,
    QDialogButtonBox, QSpinBox, QMessageBox, QCheckBox, QLabel,
    QStackedWidget, QToolButton, QFileDialog)

from .storage import AddressBook, StorageError, validate
from .session_ui import SessionWindow
from .transfer_ui import FileTransferWindow
from .scanner import ConnectionScanner
from .credentials import CredentialStore, CredentialStoreError, LoginDialog
from .ui_icons import icon, computer_icon, refresh_icon, availability_color, apply_theme, application_icon
from .window_chrome import FramelessMainWindow
from .grouped_tiles import GroupedComputerView
from .ui_dialogs import (StyledComboBox, ModernDialog, ConfirmationDialog,
                         confirm, ask_text, inform)
from .ui_dialogs import RoundedMenu as QMenu
from .rpb import RpbError, read_rpb, write_rpb
from .bookmarks import merge_bookmarks
from .power import PowerAction, PowerOutcome, POWER_DESCRIPTORS
from .power_ui import PowerWindow, create_power_menu
from .terminal_ui import TerminalWindow
from .restart_recovery import RestartRecoveryWindow
from .system_tray import SystemTrayController
from .instance_activation import ActivationServer, request_activation
from .application_identity import APP_NAME, configure_application, prepare_platform_identity, install_linux_launcher
from . import __version__

class ApplicationQuitGuard(QObject):
    """Route application/Dock Quit through the same worker-aware shutdown path."""
    def __init__(self, app, window):
        super().__init__(window)
        self.app, self.window = app, window
        self.active = True
        app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.app and event.type() == QEvent.Type.Quit and not self.window._shutdown_complete:
            self.window.request_quit()
            return True
        return False

    def stop(self):
        if self.active:
            self.active = False
            self.app.removeEventFilter(self)


class ConnectionDialog(QDialog):
    def __init__(self, groups, entry=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit connection" if entry else "Add connection")
        self.resize(420, 240)
        self.original = entry or {}
        form = QFormLayout(self)
        self.name = QLineEdit(self.original.get("name", ""))
        self.host = QLineEdit(self.original.get("host", ""))
        self.host.setPlaceholderText("Hostname or IP address")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(self.original.get("port", 4899))
        self.username = QLineEdit(self.original.get("username", ""))
        self.group = StyledComboBox()
        self.group.addItems(groups)
        self.group.setCurrentText(self.original.get("group", groups[0]))
        for label, widget in (("Name", self.name), ("Host", self.host), ("Port", self.port),
                              ("Username (optional)", self.username), ("Group", self.group)):
            form.addRow(label, widget)
        hint = QLabel("You can save your password in the OS credential vault when signing in.")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.check)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self):
        return {"id": self.original.get("id", str(uuid.uuid4())), "name": self.name.text().strip(),
                "host": self.host.text().strip(), "port": self.port.value(),
                "username": self.username.text(), "group": self.group.currentText()}

    def check(self):
        from .storage import defaults
        data = defaults()
        data["groups"] = [self.group.currentText()]
        data["connections"] = [self.value()]
        try:
            validate(data)
        except StorageError as error:
            inform(self, "Invalid connection", str(error))
            return
        self.accept()


CredentialsDialog = LoginDialog


class MainWindow(FramelessMainWindow):
    shutdown_ready = Signal()

    def __init__(self, store, data, adapter_factory=None, credential_store=None):
        super().__init__()
        self.store, self.data = store, data
        self.adapter_factory = adapter_factory
        self.sessions = set()
        self.credential_store = credential_store if credential_store is not None else CredentialStore()
        self._pending_credentials = {}
        self._power_targets = {}
        self._restart_intents = {}
        self._restart_recoveries = {}
        self._last_connection_modes = {}
        self.exiting = False
        self.tray_controller = None
        self.activation_server = None
        self.quit_guard = None
        self._shutdown_complete = False
        self._quit_poll = QTimer(self)
        self._quit_poll.setSingleShot(True)
        self._quit_poll.setInterval(100)
        self._quit_poll.timeout.connect(self.close)
        self.scan_results = {}
        self._items_by_id = {}
        self._grid_by_id = {}
        self._entries_by_id = {}
        self._group_items = {}
        self._notifications = set()
        self.scanner = ConnectionScanner(self)
        self.scanner.result.connect(self.scan_result)
        self.scanner.progress.connect(self.scan_progress)
        self.scanner.finished.connect(self.scan_finished)
        self._scan_frame = 0
        self.scan_animation = QTimer(self)
        self.scan_animation.setInterval(40)
        self.scan_animation.timeout.connect(self.animate_scan)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(application_icon())
        self.resize(1180, 620)
        toolbar = QToolBar("Connections")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.setIconSize(QSize(22, 22))
        self.addToolBar(toolbar)
        self.add_connection_action = QAction(icon("plus"), "Add computer", self)
        self.add_connection_action.setShortcut("Ctrl+N")
        self.add_connection_action.triggered.connect(self.add_connection)
        self.addAction(self.add_connection_action)
        self.add_group_action = QAction(icon("folder"), "Add group", self)
        self.add_group_action.triggered.connect(self.add_group)
        self.add_menu = QMenu(self)
        self.add_menu.addAction(self.add_connection_action)
        self.add_menu.addAction(self.add_group_action)
        self.add_button = QToolButton()
        self.add_button.setObjectName("AddButton")
        self.add_button.setDefaultAction(self.add_connection_action)
        self.add_button.setAccessibleName("Add computer or group")
        self.add_button.setMenu(self.add_menu)
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        toolbar.addWidget(self.add_button)
        self.edit_action = toolbar.addAction(icon("edit"), "Edit computer", self.edit_selected)
        self.remove_action = toolbar.addAction(icon("trash"), "Remove computer", self.remove_selected)
        toolbar.addSeparator()
        toolbar.addAction(icon("settings"), "Preferences", self.settings)
        scan_toolbar = QToolBar("Availability")
        self.addToolBar(scan_toolbar)
        scan_toolbar.setMovable(False)
        scan_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        scan_toolbar.setIconSize(QSize(22, 22))
        self.scan_action = scan_toolbar.addAction(refresh_icon(), "Refresh availability", self.toggle_scan)
        self.scan_action.setShortcut("F5")
        self.scan_action.setToolTip("Refresh availability of saved computers (F5)")
        self.view_action = QAction(icon("eye"), "View only", self)
        self.view_action.triggered.connect(lambda: self.connect_session("view"))
        self.control_action = QAction(icon("control"), "Full control", self)
        self.control_action.triggered.connect(lambda: self.connect_session("control"))
        self.transfer_action = QAction(icon("folder"), "File transfer", self)
        self.transfer_action.triggered.connect(lambda: self.connect_session("transfer"))
        self.terminal_action = QAction(icon("terminal"), "Terminal", self)
        self.terminal_action.triggered.connect(lambda: self.connect_session("terminal"))
        toolbar.addAction(self.view_action)
        toolbar.addAction(self.control_action)
        toolbar.addAction(self.transfer_action)
        toolbar.addAction(self.terminal_action)
        self.power_menu = create_power_menu(self, self.request_power)
        self.power_action = QAction(icon("power"), "Power options", self)
        power_button = QToolButton()
        power_button.setDefaultAction(self.power_action)
        power_button.setAccessibleName("Remote power options")
        power_button.setMenu(self.power_menu)
        power_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(power_button)
        self.signin_action = QAction(icon("lock"), "Sign in with different credentials…", self)
        self.signin_action.triggered.connect(lambda: self.connect_session("control", force_prompt=True))
        self.forget_action = QAction(icon("trash"), "Forget saved password", self)
        self.forget_action.triggered.connect(self.forget_password)
        self.import_action = QAction(icon("download"), "Import Radmin bookmarks…", self)
        self.import_action.triggered.connect(self.import_bookmarks)
        self.export_action = QAction(icon("upload"), "Export Radmin bookmarks…", self)
        self.export_action.triggered.connect(self.export_bookmarks)
        self.computer_menu = QMenu("Computer actions", self)
        for action in (self.control_action, self.view_action, self.transfer_action, self.terminal_action):
            self.computer_menu.addAction(action)
        self.computer_menu.addMenu(self.power_menu)
        self.computer_menu.addSeparator()
        for action in (self.edit_action, self.signin_action, self.forget_action, self.remove_action):
            self.computer_menu.addAction(action)
        self.group_menu = QMenu("Group actions", self)
        self.group_menu.addAction(self.add_connection_action)
        self.group_menu.addSeparator()
        self.group_menu.addAction(self.edit_action)
        self.group_menu.addAction(self.remove_action)
        for menu in (self.computer_menu, self.group_menu):
            menu.addSeparator()
            menu.addAction(self.import_action)
            menu.addAction(self.export_action)
        self.quit_action = QAction(icon("disconnect"), "Quit application", self)
        self.quit_action.setShortcut("Ctrl+Q")
        self.quit_action.setAutoRepeat(False)
        self.quit_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.quit_action.triggered.connect(self.request_quit)
        self.addAction(self.quit_action)
        for menu in (self.computer_menu, self.group_menu):
            menu.addSeparator()
            menu.addAction(self.quit_action)
        more = self.more_button = QToolButton()
        more.setIcon(icon("more"))
        more.setToolTip("More actions and bookmark import/export")
        more.setAccessibleName("More actions and bookmark import/export")
        more.setMenu(self.computer_menu)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(more)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        heading_row = QHBoxLayout()
        heading = QLabel("Computers")
        heading.setObjectName("Heading")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        self.group_filter = StyledComboBox()
        self.group_filter.setAccessibleName("Filter computer group")
        heading_row.addWidget(self.group_filter)
        self.view_choices = QActionGroup(self)
        self.view_choices.setExclusive(True)
        self.grid_action = QAction(icon("grid"), "Icon view", self, checkable=True)
        self.list_action = QAction(icon("list"), "Detailed list", self, checkable=True)
        self.grid_action.setChecked(True)
        for action in (self.grid_action, self.list_action):
            self.view_choices.addAction(action)
            button = QToolButton()
            button.setDefaultAction(action)
            button.setAccessibleName(action.text())
            heading_row.addWidget(button)
        layout.addLayout(heading_row)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, host, username or group…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Connection / group", "Host", "Port", "Username", "Status", "Connect time", "Last checked"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 250)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 65)
        self.tree.setColumnWidth(3, 120)
        self.tree.setColumnWidth(4, 110)
        self.tree.setColumnWidth(6, 210)
        self.tree.setSortingEnabled(True)
        self.grid = GroupedComputerView()
        self.grid.setAccessibleName("Saved computers")
        self.views = QStackedWidget()
        self.views.addWidget(self.grid)
        self.views.addWidget(self.tree)
        layout.addWidget(self.views, 1)
        self.empty_label = QLabel("Add your first computer with + to get started.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)
        self.setCentralWidget(central)
        self.search.textChanged.connect(self.refresh)
        self.group_filter.currentIndexChanged.connect(self.refresh)
        self.grid_action.triggered.connect(lambda: self.views.setCurrentWidget(self.grid))
        self.list_action.triggered.connect(lambda: self.views.setCurrentWidget(self.tree))
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.connect_session("view") if self.selected() else None)
        self.grid.selection_changed.connect(self.grid_selection_changed)
        self.grid.activated.connect(lambda key: self.connect_session("control", self._entries_by_id.get(key)))
        self.grid.context_requested.connect(self.grid_context_menu)
        self.grid.group_collapsed.connect(self.grid_group_collapsed)
        self.tree.itemCollapsed.connect(lambda item: self.tree_group_collapsed(item, True))
        self.tree.itemExpanded.connect(lambda item: self.tree_group_collapsed(item, False))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda pos: self.context_menu(self.tree, pos))
        for bar in (toolbar, scan_toolbar):
            for action in bar.actions():
                button = bar.widgetForAction(action)
                if button:
                    button.setAccessibleName(action.text() or button.accessibleName())
        self.install_chrome()
        self.menuBar().hide()
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().hide()
        self.refresh()

    def selected(self):
        item = self.tree.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return next((entry for entry in self.data["connections"] if entry["id"] == key), None)

    def selected_group(self):
        item = self.tree.currentItem()
        if item and item.parent():
            item = item.parent()
        return item.text(0) if item else self.group_filter.currentData()

    def selected_group_header(self):
        item = self.tree.currentItem()
        return item.text(0) if item is not None and item.parent() is None else None

    def edit_selected(self):
        self.rename_group() if self.selected_group_header() is not None else self.edit_connection()

    def remove_selected(self):
        self.remove_group() if self.selected_group_header() is not None else self.remove_connection()

    def selection_changed(self):
        enabled = self.selected() is not None and not self.exiting
        group = self.selected_group_header()
        for action in (self.view_action, self.control_action, self.transfer_action, self.terminal_action,
                       self.signin_action, self.forget_action, self.power_action):
            action.setEnabled(enabled)
        self.power_menu.setEnabled(enabled)
        for action in (self.edit_action, self.remove_action):
            action.setEnabled(not self.exiting and (enabled or group is not None))
        self.edit_action.setText("Rename group" if group is not None else "Edit computer")
        self.remove_action.setText("Remove group" if group is not None else "Remove computer")
        for action in (self.edit_action, self.remove_action):
            for associated in action.associatedObjects():
                if isinstance(associated, QToolButton):
                    associated.setAccessibleName(action.text())
        self.more_button.setMenu(self.group_menu if group is not None else self.computer_menu)
        entry = self.selected()
        self.grid.set_selection(entry_id=entry["id"] if entry else None, group=group)

    def grid_selection_changed(self, key, group):
        self.tree.setCurrentItem(self._items_by_id.get(key) if key else self._group_items.get(group))

    def grid_context_menu(self, key, group, point):
        self.grid_selection_changed(key, group)
        (self.computer_menu if key else self.group_menu).popup(point)

    def tree_group_collapsed(self, item, collapsed):
        if item.parent() is None:
            self.grid.set_group_collapsed(item.text(0), collapsed)

    def grid_group_collapsed(self, group, collapsed):
        item = self._group_items.get(group)
        if item is not None:
            self.tree.blockSignals(True)
            item.setExpanded(not collapsed)
            self.tree.blockSignals(False)

    def context_menu(self, widget, point):
        item = widget.itemAt(point)
        if item is not None:
            widget.setCurrentItem(item)
        if self.selected() is not None:
            self.computer_menu.popup(widget.viewport().mapToGlobal(point))
        elif self.selected_group_header() is not None:
            self.group_menu.popup(widget.viewport().mapToGlobal(point))

    def refresh(self):
        selected = self.selected()
        selected_header = self.selected_group_header()
        group_choice = self.group_filter.currentData()
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("All computers", None)
        for group in self.data["groups"]:
            self.group_filter.addItem(group, group)
        self.group_filter.setCurrentIndex(max(0, self.group_filter.findData(group_choice)))
        self.group_filter.blockSignals(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        self._items_by_id = {}
        self._grid_by_id = {}
        self._group_items = {}
        self._entries_by_id = {e["id"]: e for e in self.data["connections"]}
        self.scan_results = {key: result for key, result in self.scan_results.items()
                             if key in self._entries_by_id and
                             (result.host, result.port) == (self._entries_by_id[key]["host"], self._entries_by_id[key]["port"])}
        query = self.search.text().casefold().strip()
        visible_groups = []
        for group in self.data["groups"]:
            if self.group_filter.currentData() is not None and group != self.group_filter.currentData():
                continue
            entries = [entry for entry in self.data["connections"] if entry["group"] == group
                       and (not query or query in " ".join(str(entry[k]) for k in
                                                           ("name", "host", "port", "username", "group")).casefold())]
            if not entries and query and query not in group.casefold():
                continue
            visible_groups.append((group, entries))
            parent = QTreeWidgetItem([group])
            parent.setIcon(0, icon("folder"))
            self._group_items[group] = parent
            for entry in entries:
                child = QTreeWidgetItem([entry["name"], entry["host"], str(entry["port"]), entry["username"]])
                child.setData(0, Qt.ItemDataRole.UserRole, entry["id"])
                child.setIcon(0, icon("monitor"))
                self._items_by_id[entry["id"]] = child
                self.paint_scan_result(child, self.scan_results.get(entry["id"]))
                parent.addChild(child)
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(group not in self.grid.collapsed_groups)
        self.grid.set_groups(visible_groups)
        self._grid_by_id = dict(self.grid.items_by_id)
        for key in self._grid_by_id:
            self.paint_grid_result(self._entries_by_id[key])
        self.tree.setCurrentItem(self._items_by_id.get(selected["id"]) if selected else self._group_items.get(selected_header))
        self.tree.blockSignals(False)
        self.empty_label.setVisible(not visible_groups)
        self.empty_label.setText("No computers match your search.")
        self.selection_changed()
        self.scan_action.setEnabled((bool(self.data["connections"]) or self.scanner.running) and not self.exiting)

    def paint_grid_result(self, entry):
        tile = self._grid_by_id.get(entry["id"])
        if tile is None:
            return
        result = self.scan_results.get(entry["id"])
        status = result.status if result else "Not checked"
        tile.setIcon(computer_icon(status, self._scan_frame if status in ("Queued", "Checking…") else 0))
        detail = f"\n{result.detail}" if result and result.detail else ""
        tile.setToolTip(f"{entry['name']}\n{entry['host']}:{entry['port']}\n{status}{detail}\nDouble-click for full control. Right-click for more actions.")
        tile.setData(Qt.ItemDataRole.AccessibleDescriptionRole, f"{entry['name']}, {entry['host']}, {status}")

    def paint_scan_result(self, item, result):
        status = result.status if result else "Not checked"
        item.setText(4, status)
        item.setText(5, f"{result.latency_ms} ms" if result and result.latency_ms is not None else "")
        item.setText(6, result.checked_at.replace("T", " ") if result else "")
        item.setToolTip(4, result.detail if result else "Use Refresh availability (F5).")
        item.setToolTip(5, "Time to establish TCP, including hostname lookup; not an ICMP ping.")
        item.setForeground(4, QBrush(QColor(availability_color(status))))
        item.setIcon(4, refresh_icon(self._scan_frame) if status in ("Queued", "Checking…") else QIcon())

    def toggle_scan(self):
        if self.scanner.running:
            self.scanner.cancel()
        else:
            self.start_scan()

    def animate_scan(self):
        self._scan_frame = (self._scan_frame + 1) % 30
        self.scan_action.setIcon(refresh_icon(self._scan_frame))
        # Update existing items, never rebuild the views or disturb selection.
        for entry_id, tile in self._grid_by_id.items():
            result = self.scan_results.get(entry_id)
            if result and result.status in ("Queued", "Checking…"):
                tile.setIcon(computer_icon(result.status, self._scan_frame))
        for entry_id, item in self._items_by_id.items():
            result = self.scan_results.get(entry_id)
            if result and result.status in ("Queued", "Checking…"):
                item.setIcon(4, refresh_icon(self._scan_frame))

    def start_scan(self):
        if self.exiting or self.scanner.running or not self.data["connections"]:
            return
        self._scan_frame = 0
        self.scan_action.setIcon(refresh_icon())
        self.scan_action.setToolTip("Checking saved computers… Click to stop (F5)")
        self.scan_animation.start()
        self.scanner.start(self.data["connections"])

    def scan_result(self, result):
        entry = self._entries_by_id.get(result.entry_id)
        if entry is None or (entry["host"], entry["port"]) != (result.host, result.port):
            return  # Entry was edited/deleted while this snapshot was being checked.
        self.scan_results[result.entry_id] = result
        item = self._items_by_id.get(result.entry_id)
        if item is not None:
            self.paint_scan_result(item, result)
        self.paint_grid_result(entry)

    def scan_progress(self, done, total):
        self.scan_action.setToolTip(f"Checking saved computers ({done}/{total})… Click to stop (F5)")

    def scan_finished(self, cancelled):
        self.scan_animation.stop()
        self._scan_frame = 0
        self.scan_action.setIcon(refresh_icon())
        self.scan_action.setToolTip("Refresh availability of saved computers (F5)")
        self.scan_action.setEnabled(bool(self.data["connections"]) and not self.exiting)

    def commit(self, updated):
        try:
            self.store.save(updated)
        except StorageError as error:
            inform(self, "Save failed", str(error))
            return False
        self.data = updated
        self.refresh()
        return True

    def add_connection(self):
        dialog = ConnectionDialog(self.data["groups"], parent=self)
        if self.selected_group() is not None:
            dialog.group.setCurrentText(self.selected_group())
        if dialog.exec():
            updated = copy.deepcopy(self.data)
            updated["connections"].append(dialog.value())
            self.commit(updated)

    def edit_connection(self):
        entry = self.selected()
        if entry is None:
            return
        dialog = ConnectionDialog(self.data["groups"], entry, self)
        if dialog.exec():
            updated = copy.deepcopy(self.data)
            updated["connections"] = [dialog.value() if e["id"] == entry["id"] else e for e in updated["connections"]]
            self.commit(updated)

    def remove_connection(self):
        entry = self.selected()
        if entry and confirm(self, "Remove computer", f"Remove “{entry['name']}” from your saved computers?", "Remove computer"):
            updated = copy.deepcopy(self.data)
            updated["connections"] = [e for e in updated["connections"] if e["id"] != entry["id"]]
            self.commit(updated)

    def add_group(self):
        name, ok = ask_text(self, "Add group", "Group name")
        if ok:
            updated = copy.deepcopy(self.data)
            updated["groups"].append(name.strip())
            if self.commit(updated):
                if name.strip() not in self._group_items:
                    self.group_filter.setCurrentIndex(0)
                self.tree.setCurrentItem(self._group_items.get(name.strip()))

    def rename_group(self):
        old = self.selected_group()
        if old is None:
            return
        name, ok = ask_text(self, "Rename group", "Group name", initial=old)
        if ok:
            updated = copy.deepcopy(self.data)
            updated["groups"] = [name.strip() if group == old else group for group in updated["groups"]]
            for entry in updated["connections"]:
                if entry["group"] == old:
                    entry["group"] = name.strip()
            was_filtered = self.group_filter.currentData() == old
            was_collapsed = old in self.grid.collapsed_groups
            if self.commit(updated):
                self.grid.collapsed_groups.discard(old)
                self.grid.set_group_collapsed(name.strip(), was_collapsed, emit=True)
                if was_filtered:
                    self.group_filter.setCurrentIndex(self.group_filter.findData(name.strip()))
                self.tree.setCurrentItem(self._group_items.get(name.strip()))

    def remove_group(self):
        group = self.selected_group()
        if group is None:
            return
        if len(self.data["groups"]) == 1:
            inform(self, "Keep one group", "Add another group before removing the last one.")
            return
        members = [e for e in self.data["connections"] if e["group"] == group]
        others = [name for name in self.data["groups"] if name != group]
        message = f"Remove the group “{group}”?"
        if members:
            message += f" Its {len(members)} saved computers will be moved to the group selected below."
        dialog = ConfirmationDialog("Remove group", message, self, confirm_text="Remove group")
        destination = StyledComboBox()
        destination.addItems(others)
        if "General" in others:
            destination.setCurrentText("General")
        destination.setAccessibleName("Move computers to group")
        if members:
            dialog.body_layout.addWidget(destination)
        else:
            destination.setParent(dialog)
            destination.hide()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = copy.deepcopy(self.data)
            updated["groups"].remove(group)
            for entry in updated["connections"]:
                if entry["group"] == group:
                    entry["group"] = destination.currentText()
            if self.commit(updated):
                self.grid.collapsed_groups.discard(group)
        dialog.deleteLater()

    def settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        form = QFormLayout(dialog)
        timeout = QSpinBox()
        timeout.setRange(1, 120)
        timeout.setSuffix(" seconds")
        timeout.setValue(self.data["settings"]["timeout"])
        scaling = StyledComboBox()
        scaling.addItems(["Fit to window", "Actual pixels (scroll)"])
        scaling.setCurrentIndex(0 if self.data["settings"]["scaling"] == "fit" else 1)
        fullscreen = QCheckBox("Open sessions fullscreen")
        fullscreen.setChecked(self.data["settings"]["fullscreen"])
        form.addRow("Connection timeout", timeout)
        form.addRow("Default scaling", scaling)
        form.addRow(fullscreen)
        form.addRow(QLabel("Changes apply to new sessions. F11 toggles fullscreen; Esc exits it."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            updated = copy.deepcopy(self.data)
            updated["settings"] = {"timeout": timeout.value(), "scaling": "fit" if scaling.currentIndex() == 0 else "actual",
                                   "fullscreen": fullscreen.isChecked()}
            self.commit(updated)

    def import_bookmarks(self):
        if self.exiting:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import Radmin bookmarks", "",
                                            "Radmin phonebooks (*.rpb);;All files (*)")
        if not path:
            return
        try:
            merged, summary = merge_bookmarks(self.data, read_rpb(path))
        except (RpbError, StorageError) as error:
            inform(self, "Cannot import bookmarks", str(error))
            return
        if not summary.computers and not summary.groups:
            self.notify(f"No new bookmarks to import. {summary.duplicates} identical computers are already saved.")
            return
        message = (f"Import {summary.computers} computers and {summary.groups} new groups from “{Path(path).name}”? "
                   f"{summary.duplicates} identical computers will be skipped. Existing computers and settings will be kept.\n\n"
                   "Connection names, addresses, ports, usernames and folders are transferred. "
                   "Passwords and original Viewer display/chat preferences are not transferred.")
        if confirm(self, "Import Radmin bookmarks", message, "Import", destructive=False):
            if self.commit(merged):
                self.search.clear()
                self.group_filter.setCurrentIndex(0)

    def export_bookmarks(self):
        if self.exiting:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Radmin bookmarks", "Radmin-bookmarks.rpb",
                                            "Radmin phonebooks (*.rpb)",
                                            options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != ".rpb":
            destination = destination.with_name(destination.name + ".rpb")
        if destination.exists() and not confirm(self, "Replace bookmark file",
                                                f"Replace “{destination.name}” with the current saved bookmarks?",
                                                "Replace file"):
            return
        try:
            write_rpb(destination, self.data)
        except RpbError as error:
            inform(self, "Cannot export bookmarks", str(error))
            return
        self.notify(f"Exported {len(self.data['connections'])} computers to “{destination.name}” in original Radmin .rpb format. Passwords are not included.")

    def connect_session(self, mode, entry=None, *, force_prompt=False, error_message="", reconnect_mode=None):
        entry = self.selected() if entry is None else entry
        if entry is None or self.exiting:
            return
        entry = dict(entry)
        if not force_prompt and entry["username"] and self.credential_store.available:
            try:
                saved = self.credential_store.get(entry)
            except CredentialStoreError as error:
                self.notify(str(error))
            else:
                if saved is not None:
                    self.launch_session(mode, entry, saved, reconnect_mode=reconnect_mode)
                    return
        dialog = CredentialsDialog(entry, self, store=self.credential_store,
                                   error_message=error_message, prefill_saved=not bool(error_message))
        try:
            if dialog.exec():
                submitted = dict(entry, username=dialog.username.text())
                self.launch_session(mode, submitted, dialog.password.text(),
                                    remember=dialog.remember.isChecked(), original=entry, reconnect_mode=reconnect_mode)
        finally:
            dialog.password.clear()
            dialog.deleteLater()

    @staticmethod
    def power_target(entry):
        return entry["host"].casefold(), entry["port"]

    def request_power(self, action, entry=None, *, force_prompt=False, error_message="", reconnect_mode=None):
        entry = self.selected() if entry is None else entry
        if entry is None or self.exiting:
            return
        entry = dict(entry)
        action = PowerAction(action)
        if self.power_target(entry) in self._restart_recoveries:
            self.notify("A restart recovery window is already open for this computer. Close it before sending another power request.")
            return
        if self.power_target(entry) in self._power_targets:
            self.notify("A power request window is already open for this computer. Close it before starting another request.")
            return
        descriptor = POWER_DESCRIPTORS[action]
        message = (f"{descriptor.label} “{entry['name']}” ({entry['host']}:{entry['port']})?\n\n"
                   f"{descriptor.confirmation}\n\nRequires: {descriptor.permission}.")
        if action == PowerAction.RESTART:
            reconnect_mode = reconnect_mode or self._last_connection_modes.get(self.power_target(entry), "control")
            if reconnect_mode not in ("view", "control", "terminal"):
                reconnect_mode = "control"
            label = {"view": "view-only desktop", "control": "desktop control", "terminal": "terminal"}[reconnect_mode]
            message += f"\n\nAfter restart, wait up to five minutes for the computer to return, then reopen {label}. You can cancel recovery at any time."
        if confirm(self, descriptor.label + " remote computer", message,
                   descriptor.label, destructive=descriptor.may_force_close):
            self.connect_session("power:" + action.value, entry,
                                 force_prompt=force_prompt, error_message=error_message,
                                 reconnect_mode=reconnect_mode if action == PowerAction.RESTART else None)

    def launch_session(self, mode, entry, password, *, remember=None, original=None, reconnect_mode=None):
        if mode.startswith("power:"):
            key = self.power_target(entry)
            if key in self._power_targets or key in self._restart_recoveries:
                self.notify("A power request is already open for this computer.")
                return None
            window = PowerWindow(entry, password, self.data["settings"], PowerAction(mode.split(":", 1)[1]))
            self._power_targets[key] = window
            if mode == "power:restart":
                resume_mode = reconnect_mode or self._last_connection_modes.get(key, "control")
                if resume_mode not in ("view", "control", "terminal"):
                    resume_mode = "control"
                self._restart_intents[window] = (dict(entry), password, resume_mode)
        elif mode == "transfer":
            window = FileTransferWindow(entry, password, self.data["settings"])
        elif mode == "terminal":
            window = TerminalWindow(entry, password, self.data["settings"])
        else:
            window = SessionWindow(entry, password, self.data["settings"], mode, self.adapter_factory)
        if mode in ("view", "control", "terminal"):
            self._last_connection_modes[self.power_target(entry)] = mode
        self.sessions.add(window)
        if remember is not None:
            # Keep the save intent only until verified authentication or teardown.
            self._pending_credentials[window] = (dict(entry), password if remember else "", remember, original)
        window.authenticated.connect(lambda w=window: self.credentials_verified(w))
        window.worker.finished.connect(lambda w=window: self._pending_credentials.pop(w, None))
        if hasattr(window, "operation_finished"):
            window.operation_finished.connect(lambda w=window: self._pending_credentials.pop(w, None))
        if mode == "power:restart":
            window.operation_finished.connect(lambda w=window: self.restart_dispatched(w))
        window.retry_requested.connect(lambda e, m, msg, w=window: self.retry_session(w, e, m, msg))
        if hasattr(window, "session_error"):
            window.session_error.connect(self.notify)
        if hasattr(window, "power_requested"):
            window.power_requested.connect(lambda target, action, source_mode=mode:
                                           self.request_power(action, target, reconnect_mode=source_mode))
        if hasattr(window, "terminal_requested"):
            window.terminal_requested.connect(lambda target: self.connect_session("terminal", target))
        if hasattr(window, "transfer_requested"):
            window.transfer_requested.connect(lambda target: self.connect_session("transfer", target))
        window.destroyed.connect(lambda: self.session_destroyed(window))
        window.show()
        return window

    def session_destroyed(self, window):
        self.sessions.discard(window)
        self._pending_credentials.pop(window, None)
        self._restart_intents.pop(window, None)
        self._power_targets = {target: owner for target, owner in self._power_targets.items() if owner is not window}
        self._restart_recoveries = {target: owner for target, owner in self._restart_recoveries.items() if owner is not window}

    def restart_dispatched(self, window):
        intent = self._restart_intents.pop(window, None)
        if intent is None or self.exiting or window.closing or window.worker.cancel.is_set():
            return
        result = window.result_value
        if result is None or not result.dispatched or result.outcome not in (PowerOutcome.ACCEPTED, PowerOutcome.UNKNOWN):
            return
        entry, password, mode = intent
        target = self.power_target(entry)
        # Retire the old sockets even if a reboot dropped TCP without sending FIN.
        # Nothing in a terminal command queue or transfer is replayed on recovery.
        for existing in list(self.sessions):
            if existing is not window and isinstance(existing, (SessionWindow, TerminalWindow, FileTransferWindow)):
                if self.power_target(existing.entry) == target:
                    existing.close()
        recovery = RestartRecoveryWindow(entry, password, mode)
        self._restart_recoveries[target] = recovery
        self.sessions.add(recovery)
        recovery.ready.connect(lambda e, p, m, owner=recovery: self.restart_ready(owner, e, p, m))
        recovery.destroyed.connect(lambda: self.session_destroyed(recovery))
        recovery.show()
        window.close()

    def restart_ready(self, recovery, entry, password, mode):
        target = self.power_target(entry)
        if self.exiting or recovery.closing or self._restart_recoveries.get(target) is not recovery:
            return
        self._restart_recoveries.pop(target, None)
        # Respect a manual reconnection made while the recovery window was open.
        for existing in self.sessions:
            if isinstance(existing, (SessionWindow, TerminalWindow)) and not existing.closing:
                if (getattr(existing, "mode", None) == mode and self.power_target(existing.entry) == target
                        and existing.worker.isRunning() and not getattr(existing, "failed_session", False)):
                    existing.raise_()
                    existing.activateWindow()
                    return
        self.launch_session(mode, entry, password)

    def credentials_verified(self, window):
        intent = self._pending_credentials.pop(window, None)
        if intent is None or self.exiting or window.closing or not self.credential_store.available:
            return
        entry, password, remember, original = intent
        try:
            if remember:
                self.credential_store.save(entry, password)
            else:
                self.credential_store.forget(entry)
                if original and original["username"] != entry["username"]:
                    self.credential_store.forget(original)
        except CredentialStoreError as error:
            self.notify(str(error))
            return
        if remember:
            # Remember the login name in the existing username field, never its secret.
            updated = copy.deepcopy(self.data)
            for saved in updated["connections"]:
                if (saved["id"], saved["host"], saved["port"]) == (entry["id"], entry["host"], entry["port"]):
                    if saved["username"] != entry["username"]:
                        saved["username"] = entry["username"]
                        self.commit(updated)
                    break

    def retry_session(self, window, entry, mode, message):
        self._pending_credentials.pop(window, None)
        if mode.startswith("power:"):
            intent = self._restart_intents.pop(window, None)
            resume_mode = intent[2] if intent else self._last_connection_modes.get(self.power_target(entry), "control")
            self._power_targets = {target: owner for target, owner in self._power_targets.items() if owner is not window}
            # Authentication failed before dispatch: a new attempt still needs
            # explicit power confirmation and an explicitly submitted login.
            QTimer.singleShot(0, lambda: self.request_power(PowerAction(mode.split(":", 1)[1]), entry,
                                                          force_prompt=True, error_message=message,
                                                          reconnect_mode=resume_mode)
                              if not self.exiting else None)
            return
        # Let the old window close before opening a fresh credential prompt.
        QTimer.singleShot(0, lambda: self.connect_session(mode, entry, force_prompt=True, error_message=message)
                          if not self.exiting else None)

    def forget_password(self):
        entry = self.selected()
        if entry is None:
            return
        try:
            self.credential_store.forget(entry)
        except CredentialStoreError as error:
            self.notify(str(error))
        else:
            self.notify("Saved password removed for this host, port and username.")

    def notify(self, message):
        """Action failures remain visible without a permanent footer/status strip."""
        if self.exiting:
            return
        dialog = ModernDialog("Connection notice", self)
        dialog.add_message(message)
        dialog.add_button("OK", dialog.accept, default=True)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._notifications.add(dialog)
        dialog.destroyed.connect(lambda: self._notifications.discard(dialog))
        dialog.open()

    def enable_tray(self, **kwargs):
        """Runtime opt-in; direct window tests can retain ordinary close semantics."""
        if self.tray_controller is None:
            self.tray_controller = SystemTrayController(self, **kwargs)
            self.tray_controller.quit_requested.connect(self.request_quit)
            self.tray_controller.availability_changed.connect(self._tray_availability)
            self._tray_availability(self.tray_controller.available)
        return self.tray_controller

    def _tray_availability(self, available):
        label = "Hide to system tray" if available else "Close"
        self.chrome.close_button.setToolTip(label)
        self.chrome.close_button.setAccessibleName(label)

    def handle_activation(self, payload):
        if self.exiting:
            return
        if self.tray_controller is not None:
            self.tray_controller.restore()
        else:
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            self.show()
            self.raise_()
            self.activateWindow()
        if payload is not None:
            QTimer.singleShot(0, lambda: self.connect_session(payload["mode"], payload["quick_entry"])
                              if not self.exiting else None)

    def request_quit(self):
        if self.exiting:
            return
        self.exiting = True
        if self.tray_controller is not None:
            self.tray_controller.begin_shutdown()
        self.close()

    def closeEvent(self, event):
        if not self.exiting and self.tray_controller is not None and self.tray_controller.hide_to_tray():
            event.ignore()
            return
        self.exiting = True
        self.quit_action.setEnabled(False)
        if self.tray_controller is not None:
            self.tray_controller.begin_shutdown()
        self.scanner.cancel()
        for dialog in list(self._notifications):
            dialog.close()
        if self.sessions:
            self.exiting = True
            self.setEnabled(False)
            for window in list(self.sessions):
                window.close()
            self._quit_poll.start()
            event.ignore()
        else:
            self._quit_poll.stop()
            event.accept()
            if not self._shutdown_complete:
                self._shutdown_complete = True
                if self.tray_controller is not None:
                    self.tray_controller.stop()
                if self.activation_server is not None:
                    self.activation_server.stop()
                if self.quit_guard is not None:
                    self.quit_guard.stop()
                self.shutdown_ready.emit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--smoke-test", action="store_true", help="Check bundled resources and native modules without connecting or loading user data")
    parser.add_argument("--smoke-report", type=Path, help="Write smoke-check JSON to this path (requires --smoke-test)")
    parser.add_argument("--config", type=Path, help="Address book JSON path (default: platform app config directory)")
    parser.add_argument("--install-desktop-entry", action="store_true", help="Install/update this user's Linux launcher and application icon, then exit")
    parser.add_argument("--host", help="Unsaved quick-connect target; opens a password dialog without automatic authentication")
    parser.add_argument("--port", type=int, help="Quick-connect port (default: 4899; requires --host)")
    parser.add_argument("--username", help="Prefill the quick-connect username (requires --host)")
    parser.add_argument("--mode", choices=("view", "control", "transfer", "terminal"), default="view",
                        help="Quick-connect mode (default: view)")
    args = parser.parse_args(argv)
    if args.smoke_report is not None and not args.smoke_test:
        parser.error("--smoke-report requires --smoke-test")
    if args.install_desktop_entry:
        try:
            print(install_linux_launcher())
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return 0
    if args.host is None and (args.port is not None or args.username is not None):
        parser.error("--port and --username require --host")
    quick_entry = None
    if args.host is not None:
        from .storage import defaults
        quick_entry = {"id": str(uuid.uuid4()), "name": args.host, "host": args.host,
                       "port": args.port if args.port is not None else 4899,
                       "username": args.username or "", "group": "General"}
        check = defaults()
        check["connections"] = [quick_entry]
        try:
            validate(check)
        except StorageError as error:
            parser.error(str(error))
    prepare_platform_identity()
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    configure_application(app)
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    if args.smoke_test:
        from .runtime_check import run
        return run(app, args.smoke_report)
    path = args.config or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "connections.json"
    path = path.expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.critical(None, APP_NAME, "Cannot create the configuration directory.")
        return 1
    lock = QLockFile(str(path) + ".lock")
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if request_activation(path, quick_entry, args.mode):
            return 0
        QMessageBox.critical(None, APP_NAME, "This address book is in use by another viewer, or cannot be locked.")
        return 1
    try:
        store = AddressBook(path)
        data = store.load()
    except StorageError as error:
        QMessageBox.critical(None, "Address book could not be opened", str(error) + "\n\nThe file has not been changed. Repair it or select a different --config path.")
        lock.unlock()
        return 1
    from .adapter import DesktopAdapter
    window = MainWindow(store, data, adapter_factory=DesktopAdapter)
    window.shutdown_ready.connect(app.quit)
    window.quit_guard = ApplicationQuitGuard(app, window)
    try:
        window.activation_server = ActivationServer(path, window)
        window.activation_server.activate_requested.connect(window.handle_activation)
        window.activation_server.start()
    except OSError:
        window.activation_server = None
    window.enable_tray()
    window.show()
    if quick_entry is not None:
        QTimer.singleShot(0, lambda: window.connect_session(args.mode, quick_entry))
    try:
        return app.exec()
    finally:
        if window.tray_controller is not None:
            window.tray_controller.stop()
        if window.activation_server is not None:
            window.activation_server.stop()
        window.quit_guard.stop()
        lock.unlock()
