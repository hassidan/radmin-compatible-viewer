"""Grouped, wrapping computer tiles with a single selection and outer scrolling."""
from pathlib import Path
from functools import lru_cache

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
    QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .ui_icons import computer_icon, icon


@lru_cache(maxsize=2)
def _group_icon(collapsed):
    """A decorative chevron and folder, painted as one header icon."""
    arrow = QIcon(str(Path(__file__).with_name("assets") /
                      ("chevron-right.svg" if collapsed else "chevron-down.svg")))
    result = QIcon()
    for scale in (1, 2, 3):
        pixmap = QPixmap(44 * scale, 20 * scale)
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.drawPixmap(0, 1, 18, 18, arrow.pixmap(18 * scale, 18 * scale))
        painter.drawPixmap(26, 1, 18, 18, icon("folder").pixmap(18 * scale, 18 * scale))
        painter.end()
        result.addPixmap(pixmap)
    return result


class _TileList(QListWidget):
    geometry_changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setIconSize(QSize(72, 72))
        self.setGridSize(QSize(156, 140))
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QListWidget { border: none; border-radius: 0; "
                           "background: transparent; padding: 0; outline: none; }")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.geometry_changed.emit()

    def wheelEvent(self, event):
        # Even a scrollbar-less item view consumes wheel events by default.
        event.ignore()

    def fit_rows(self):
        self.setGridSize(QSize(min(156, max(1, self.viewport().width())), 140))
        self.doItemsLayout()
        # Use Qt's actual wrapping rather than a second, approximate column count.
        bottom = max((self.visualItemRect(self.item(i)).bottom() + 1
                      for i in range(self.count())), default=0)
        # Rects describe the painted item, which can be shorter than its cell.
        # Include the whole final row or Qt retains a hidden scroll range.
        row_height = self.gridSize().height()
        self.setFixedHeight(((bottom + row_height - 1) // row_height) * row_height)
        self.verticalScrollBar().setValue(0)


class _Section(QWidget):
    def __init__(self, name, owner):
        super().__init__(owner.widget())
        self.name = name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QToolButton(self)
        self.header.setObjectName("GroupHeader")
        self.header.setStyleSheet('''
            QToolButton#GroupHeader { background: #e9eef7; border: 1px solid transparent;
                border-radius: 9px; padding: 9px 12px; color: #253247;
                text-align: left; font-weight: 600; }
            QToolButton#GroupHeader:hover { background: #dfe7f4; }
            QToolButton#GroupHeader:focus { border: 1px solid #6188da; }
            QToolButton#GroupHeader:checked { background: #dbe8ff; color: #234eac; }
        ''')
        self.title = self.header
        self.title.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.title.setIconSize(QSize(44, 20))
        self.title.setCheckable(True)
        self.title.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setMinimumWidth(0)
        layout.addWidget(self.header)
        self.tiles = _TileList(self)
        layout.addWidget(self.tiles)
        self.title.clicked.connect(lambda: self.activate_header(owner))
        self.tiles.itemSelectionChanged.connect(lambda: owner._child_selected(self))
        self.tiles.itemActivated.connect(lambda item: owner.activated.emit(owner._id(item)))
        self.tiles.geometry_changed.connect(owner._schedule_layout)
        self.header.setProperty("groupName", name)
        self.header.installEventFilter(owner)
        self.header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header.customContextMenuRequested.connect(
            lambda pos: owner._context(self, None, self.header.mapToGlobal(pos)))
        self.tiles.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tiles.customContextMenuRequested.connect(
            lambda pos: owner._context(self, self.tiles.itemAt(pos),
                                       self.tiles.viewport().mapToGlobal(pos)))

    def activate_header(self, owner):
        owner.set_selection(group=self.name, emit=True)
        owner.set_group_collapsed(self.name, self.name not in owner.collapsed_groups, emit=True)

    def update_header(self, collapsed):
        self.title.setText(f"{self.name} ({self.tiles.count()})")
        self.title.setAccessibleName(f"{self.name}, {self.tiles.count()} computers")
        self.header.setIcon(_group_icon(collapsed))
        self.title.setAccessibleDescription("Collapsed group; activate to expand" if collapsed else "Expanded group; activate to collapse")
        self.tiles.setVisible(not collapsed and self.tiles.count() > 0)

    def select_header(self, selected):
        self.title.setChecked(selected)
        self.header.setProperty("groupSelected", selected)
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)
        self.header.update()


class GroupedComputerView(QScrollArea):
    """Entry IDs and group names must be unique within each supplied snapshot.

    UserRole holds the entry ID. Selection signals carry the owning group for a
    computer, or an empty ID for a group title. An empty pair means no selection.
    Programmatic changes and rebuilds are silent unless ``emit=True`` is passed.
    Collapse names survive clear/filter/rebuild. Items with surviving IDs are
    reused by set_groups, preserving externally applied scan decorations/data.
    """

    selection_changed = Signal(str, str)
    activated = Signal(str)
    context_requested = Signal(str, str, QPoint)
    group_collapsed = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items_by_id = {}
        self.collapsed_groups = set()
        self._sections = {}
        self._item_groups = {}
        self._items = []
        self._selection = ("", "")
        self._syncing = False
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setWidget(content)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._fit_lists)

    @staticmethod
    def _id(item):
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _schedule_layout(self):
        self._layout_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_layout_timer"):
            self._schedule_layout()

    def _fit_lists(self):
        self._layout.activate()
        for section in self._sections.values():
            if not section.tiles.isHidden():
                section.tiles.fit_rows()

    def clear(self):
        """Clear visible items and selection, retaining collapsed group names."""
        self.set_groups([])

    def set_groups(self, groups):
        selection = self._selection
        self._syncing = True
        # Detach before deleting lists: QListWidget owns its items.
        old_items = {}
        for section in self._sections.values():
            while section.tiles.count():
                item = section.tiles.takeItem(0)
                old_items[self._id(item)] = item
            self._layout.removeWidget(section)
            section.hide()
            section.deleteLater()
        self._sections.clear()
        self.items_by_id.clear()
        self._item_groups.clear()
        self._items.clear()
        for name, entries in groups:
            section = _Section(name, self)
            self._sections[name] = section
            self._layout.insertWidget(self._layout.count() - 1, section)
            for entry in entries:
                entry_id = entry["id"]
                item = old_items.pop(entry_id, None)
                if item is None:
                    item = QListWidgetItem()
                    item.setIcon(computer_icon())
                    item.setData(Qt.ItemDataRole.UserRole, entry_id)
                item.setText(entry["name"])
                section.tiles.addItem(item)
                item.setSelected(False)
                self.items_by_id[entry_id] = item
                self._item_groups[entry_id] = name
                self._items.append(item)
            section.update_header(name in self.collapsed_groups)
        self._syncing = False
        self.set_selection(entry_id=selection[0] or None, group=selection[1])
        self._schedule_layout()

    def set_group_collapsed(self, name, collapsed, emit=False):
        collapsed = bool(collapsed)
        changed = (name in self.collapsed_groups) != collapsed
        if collapsed:
            self.collapsed_groups.add(name)
        else:
            self.collapsed_groups.discard(name)
        section = self._sections.get(name)
        if section is not None:
            section.update_header(collapsed)
            self._schedule_layout()
        if changed and emit:
            self.group_collapsed.emit(name, collapsed)

    def set_selection(self, entry_id=None, group=None, emit=False):
        if self._syncing:
            return
        if entry_id:
            selection = ((entry_id, self._item_groups[entry_id])
                         if entry_id in self.items_by_id else ("", ""))
        else:
            selection = ("", group) if group in self._sections else ("", "")
        changed = selection != self._selection
        self._selection = selection
        self._syncing = True
        try:
            for name, section in self._sections.items():
                section.tiles.clearSelection()
                section.tiles.setCurrentItem(None)
                section.select_header(not selection[0] and selection[1] == name)
            if selection[0]:
                self.items_by_id[selection[0]].listWidget().setCurrentItem(
                    self.items_by_id[selection[0]])
        finally:
            self._syncing = False
        if changed and emit:
            self.selection_changed.emit(*selection)

    def _child_selected(self, section):
        if self._syncing:
            return
        selected = section.tiles.selectedItems()
        if selected:
            item = selected[0]
            self.set_selection(self._id(item), emit=True)
            rect = section.tiles.visualItemRect(item)
            center = section.tiles.viewport().mapTo(self.widget(), rect.center())
            self.ensureVisible(center.x(), center.y(), rect.width() // 2, rect.height() // 2)
        elif self._selection[0] and self._selection[1] == section.name:
            self.set_selection(emit=True)

    def _context(self, section, item, global_pos):
        entry_id = self._id(item) if item is not None else ""
        self.set_selection(entry_id, section.name, emit=True)
        self.context_requested.emit(entry_id, section.name, global_pos)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            name = watched.property("groupName")
            if name in self._sections:
                key = event.key()
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                    self.set_selection(group=name, emit=True)
                    self.set_group_collapsed(name, key == Qt.Key.Key_Left, emit=True)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    watched.click()
                    return True
        if event.type() == QEvent.Type.FocusIn:
            self.ensureWidgetVisible(watched)
        return super().eventFilter(watched, event)

    def count(self):
        return len(self._items)

    def item(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def currentItem(self):
        return self.items_by_id.get(self._selection[0])

    def setCurrentItem(self, item):
        self.set_selection(self._id(item) if item is not None else None, emit=True)

    def setCurrentRow(self, index):
        self.setCurrentItem(self.item(index))
